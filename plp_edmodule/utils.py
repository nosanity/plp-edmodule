# coding: utf-8

import logging
import requests
import types
import random
import string
from collections import defaultdict
from django.db.models import Count, Sum
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import ugettext as _
from raven import Client
from plp.utils.edx_enrollment import EDXEnrollment, EDXNotAvailable, EDXCommunicationError, EDXEnrollmentError
from plp.models import CourseSession, Participant
from plp_extension.apps.course_extension.models import CourseExtendedParameters
from plp_extension.apps.module_extension.models import EducationalModuleExtendedParameters
from .models import PromoCode, EducationalModuleProgress, EducationalModule, EducationalModuleEnrollment

RAVEN_CONFIG = getattr(settings, 'RAVEN_CONFIG', {})
client = None

if RAVEN_CONFIG:
    client = Client(RAVEN_CONFIG.get('dsn'))

REQUEST_TIMEOUT = 10
DEFAULT_PROMOCODE_LENGTH = 6
STARTED = 'started'
SCHEDULED = 'scheduled'
ENDED = 'ended'


class EDXTimeoutError(EDXEnrollmentError):
    pass


class EDXEnrollmentExtension(EDXEnrollment):
    """
    расширение класса EDXEnrollment с обработкой таймаута
    """
    def request(self, path, method='GET', **kwargs):
        url = '%s%s' % (self.base_url, path)

        headers = kwargs.setdefault('headers', {})

        if self.access_token:
            headers["Authorization"] = "Bearer %s" % self.access_token
        else:
            headers["X-Edx-Api-Key"] = settings.EDX_API_KEY
        if method == 'POST':
            headers['Content-Type'] = 'application/json'

        kwargs_copy = kwargs.copy()
        kwargs_copy.pop('headers', None)
        error_data = {
            'path': path,
            'method': method,
            'data': kwargs_copy,
        }
        try:
            logging.debug("EDXEnrollment.request %s %s %s", method, url, kwargs)
            r = self.session.request(method=method, url=url, **kwargs)
        except requests.exceptions.Timeout:
            if client:
                client.captureMessage('EDX connection timeout', extra=error_data)
            logging.error('Edx connection timeout error: %s' % error_data)
            raise EDXTimeoutError('')
        except IOError as exc:
            error_data['exception'] = str(exc)
            if client:
                client.captureMessage('EDXNotAvailable', extra=error_data)
            logging.error('EDXNotAvailable error: %s' % error_data)
            raise EDXNotAvailable("Error: {}".format(exc))

        logging.debug("EDXEnrollment.request response=%s %s", r.status_code, r.content)

        error_data.update({'status_code': r.status_code, 'content': r.content})
        if 500 <= r.status_code:
            if client:
                client.captureMessage('EDXNotAvailable', extra=error_data)
            logging.error('EDXNotAvailable error: %s' % error_data)
            raise EDXNotAvailable("Invalid EDX http response: {} {}".format(r.status_code, r.content))

        if r.status_code != 200:
            if client:
                client.captureMessage('EDXCommunicationError', extra=error_data)
            logging.error('EDXCommunicationError error: %s' % error_data)
            raise EDXCommunicationError("Invalid EDX http response: {} {}".format(r.status_code, r.content))

        return r

    def get_courses_progress(self, user_id, course_ids, timeout=REQUEST_TIMEOUT):
        query = 'user_id={}&course_id={}'.format(
            user_id,
            ','.join(course_ids)
        )
        return self.request(
            method='GET',
            path='/api/extended/edmoduleprogress?{}'.format(query),
            timeout=timeout
        )


def update_module_enrollment_progress(enrollment):
    """
    обновление прогресса из edx по сессиям курсов, входящих в модуль, на который записан пользователь
    """
    #TODO: удалить или обновить логику
    return
    module = enrollment.module
    sessions = CourseSession.objects.filter(course__in=module.courses.all())
    course_ids = [s.get_absolute_slug_v1() for s in sessions if s.course_status().get('code') == STARTED]
    try:
        data = EDXEnrollmentExtension(None).get_courses_progress(enrollment.user.username, course_ids).json()
        now = timezone.now().strftime('%H:%M:%S %Y-%m-%d')
        for k, v in data.items():
            v['updated_at'] = now
        try:
            progress = EducationalModuleProgress.objects.get(enrollment=enrollment)
            p = progress.progress or {}
            p.update(data)
            progress.progress = p
            progress.save()
        except EducationalModuleProgress.DoesNotExist:
            EducationalModuleProgress.objects.create(enrollment=enrollment, progress=data)
    except EDXEnrollmentError:
        pass


def get_feedback_list(module):
    if getattr(settings, 'ENABLE_EDMODULE_RATING', False):
        from plp_extension.apps.edmodule_review.utils import get_edmodule_feedback_list
        return get_edmodule_feedback_list(module)


def get_status_dict(session):
    """
    Статус сессии для отрисовки в шаблоне
    """
    months = {
        1: _('января'),
        2: _('февраля'),
        3: _('марта'),
        4: _('апреля'),
        5: _('мая'),
        6: _('июня'),
        7: _('июля'),
        8: _('августа'),
        9: _('сенятбря'),
        10: _('октября'),
        11: _('ноября'),
        12: _('декабря'),
    }
    if session:
        status = session.course_status()
        d = {'status': status['code']}
        if status['code'] == SCHEDULED:
            starts = timezone.localtime(session.datetime_starts).date()
            d['days_before_start'] = (starts - timezone.now().date()).days
            d['date'] = session.datetime_starts.strftime('%d.%m.%Y')
            day, month = starts.day, months.get(starts.month)
            d['date_words'] = _('начало {day} {month}').format(day=day, month=month)
        elif status['code'] == STARTED:
            ends = timezone.localtime(session.datetime_end_enroll)
            d['date'] = ends.strftime('%d.%m.%Y')
            day, month = ends.day, months.get(ends.month)
            d['date_words'] = _('запись до {day} {month}').format(day=day, month=month)
        if session.datetime_end_enroll:
            d['days_to_enroll'] = (session.datetime_end_enroll.date() - timezone.now().date()).days
        return d
    else:
        return {'status': ''}


def choose_closest_session(c):
    sessions = c.course_sessions.all()
    if sessions:
        sessions = [x for x in sessions if x.datetime_end_enroll and x.datetime_end_enroll > timezone.now()
                                and x.datetime_starts]
        sessions = sorted(sessions, key=lambda x: x.datetime_end_enroll)
        if sessions:
            return sessions[0]
    return None


def course_set_attrs(instance):
    """
    копируем атрибуты связанного CourseExtendedParams, добавляем методы
    """
    def _get_next_session(self):
        return choose_closest_session(self)

    def _get_course_status_params(self):
        return get_status_dict(self.get_next_session())

    def _get_requirements(self):
        return _string_splitter(self, 'requirements')

    def _get_profit(self):
        return _string_splitter(self, 'profit')

    def _get_documents(self):
        return _string_splitter(self, 'documents')

    def _string_splitter(obj, attr):
        try:
            ext = getattr(obj, 'extended_params')
            s = getattr(ext, attr)
            if s:
                return [i.strip() for i in s.splitlines() if i.strip()]
            return []
        except CourseExtendedParameters.DoesNotExist:
            return []

    def _get_authors_and_partners(self):
        try:
            extended = self.extended_params
            result = []
            for i in list(extended.authors.all()) + list(extended.partners.all()):
                if i not in result:
                    result.append(i)
            return result
        except CourseExtendedParameters.DoesNotExist:
            return []

    def _get_comps(self):
        result = []
        return result

    def _get_course_format_list(self):
        val = self.course_format or ''
        return [i.strip() for i in val.splitlines() if i.strip()]

    new_methods = {
        'get_next_session': _get_next_session,
        'course_status_params': _get_course_status_params,
        'get_requirements': _get_requirements,
        'get_profit': _get_profit,
        'get_documents': _get_documents,
        'get_authors_and_partners': _get_authors_and_partners,
        'get_competencies': _get_comps,
        'get_course_format_list': _get_course_format_list,
    }

    for name, method in new_methods.items():
        setattr(instance, name, types.MethodType(method, instance))

    try:
        ext = instance.extended_params
    except CourseExtendedParameters.DoesNotExist:
        ext = None
    for field in CourseExtendedParameters._meta.fields:
        if not field.auto_created and field.editable:
            if ext:
                setattr(instance, field.name, getattr(ext, field.name))
            else:
                setattr(instance, field.name, None)

    return instance


def button_status_project(session, user):
    """
    хелпер для использования в CourseSession.button_status
    """
    status = {'code': 'project_button', 'active': False, 'is_authenticated': user.is_authenticated}
    containing_module = EducationalModule.objects.filter(courses__id=session.course.id).first()
    if containing_module:
        may_enroll = containing_module.may_enroll_on_project(user)
        text = _('Запись на проект в рамках <a href="{link}">модуля</a> доступна при успешном '
                 'прохождении всех курсов модуля').format(
                link=reverse('edmodule-page', kwargs={'code': containing_module.code}))
        status.update({'text': text, 'active': may_enroll})
    return status


def update_modules_graduation(user, sessions):
    """
    Апдейт EducationalModuleEnrollment.is_graduated по оконченным сессиям по контексту моих курсов,
    т.е. у sessions должен быть параметр certificate_data
    """
    not_passed_modules = {}
    for enr in EducationalModuleEnrollment.objects.filter(user=user, is_graduated=False):
        not_passed_modules[enr.id] = set(enr.module.courses.values_list('id', flat=True))
    passed_courses = set()
    for s in sessions:
        if getattr(s, 'certificate_data', None) and s.certificate_data.get('passed'):
            passed_courses.add(s.course_id)
    to_update = []
    for m_id, courses in not_passed_modules.items():
        if courses.issubset(passed_courses):
            to_update.append(m_id)
    EducationalModuleEnrollment.objects.filter(user=user, id__in=to_update).update(is_graduated=True)


def count_user_score(user):
    """
    Подсчет баллов пользователя за пройденные курсы и модули
    """
    passed_courses = Participant.objects.filter(user=user, is_graduate=True).values_list('session__course__id', flat=True).distinct()
    courses = CourseExtendedParameters.objects.filter(course__id__in=passed_courses).aggregate(score=Sum('course_experience'))
    course_score = courses['score'] or 0
    passed_modules = EducationalModuleEnrollment.objects.filter(user=user, is_graduated=True).values_list('module__id')
    modules = EducationalModuleExtendedParameters.objects.filter(module__id__in=passed_modules).aggregate(score=Sum('em_experience'))
    module_score = modules['score'] or 0
    return {
        'module_score': module_score,
        'passed_modules': len(passed_modules),
        'passed_courses': len(passed_courses),
        'course_score': course_score,
        'whole_score': course_score + module_score,
    }

def generate_promocode(iter=0):
    promocode = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(DEFAULT_PROMOCODE_LENGTH))
    if iter > 100:
        raise Exception('Can\'t generate unique promocode')
    if PromoCode.objects.filter(code=promocode):
        iter += 1
        return generate_promocode(iter)
    else:
        return promocode

