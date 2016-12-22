# coding: utf-8

from itertools import groupby
from django.contrib import admin
from django.contrib.admin.actions import delete_selected as delete_selected_original
from django.contrib.admin.utils import quote
from django.contrib.contenttypes.admin import GenericStackedInline
from django.http import HttpResponseRedirect
from django.core.urlresolvers import reverse
from django import forms
from django.utils.translation import ugettext_lazy as _, ungettext_lazy
from autocomplete_light import modelform_factory
from statistics.admin import RemoveDeleteActionMixin
from plp_extension.apps.course_extension.models import CourseExtendedParameters
from plp_extension.apps.module_extension.admin import EducationalModuleExtendedInline
from opro_payments.admin_forms import UpsaleFormCheckerMixin
from .models import (
    EducationalModule,
    EducationalModuleEnrollment,
    EducationalModuleEnrollmentType,
    EducationalModuleEnrollmentReason,
    Benefit,
    BenefitLink,
    CoursePromotion,
)


class BenefitLinkInline(GenericStackedInline):
    model = BenefitLink
    extra = 0


class EducationalModuleAdminForm(forms.ModelForm):
    class Meta:
        model = EducationalModule
        fields = '__all__'

    def clean_courses(self):
        courses = self.cleaned_data.get('courses')
        if courses:
            proj = CourseExtendedParameters.objects.filter(course__id__in=[i.id for i in courses], is_project=True)
            if proj:
                for p in proj:
                    if EducationalModule.objects.filter(courses=p.course).count() > 0:
                        raise forms.ValidationError(_(u'Проект {title} уже содержится в другом модуле').format(
                            title=p.course.title
                        ))
        return courses

    def clean_subtitle(self):
        val = self.cleaned_data.get('subtitle')
        if val and not (1 <= len([i for i in val.splitlines() if i.strip()]) <= 3):
            raise forms.ValidationError(_(u'Введите от 1 до 3 элементов, каждый с новой строки'))
        return val


class EducationalModuleAdmin(RemoveDeleteActionMixin, admin.ModelAdmin):
    form = EducationalModuleAdminForm
    inlines = [EducationalModuleExtendedInline, BenefitLinkInline]
    readonly_fields = ('sum_ratings', 'count_ratings')


class EducationalModuleEnrollmentAdmin(admin.ModelAdmin):
    list_display = ('user', 'module', 'is_active')
    form = modelform_factory(EducationalModuleEnrollment, exclude=[])


class EducationalModuleEnrollmentReasonAdmin(admin.ModelAdmin):
    search_fields = ('enrollment__user__username', 'enrollment__user__email')
    list_display = ('enrollment', 'module_enrollment_type', )
    list_filter = ('enrollment__module__code', )


class BenefitForm(UpsaleFormCheckerMixin, forms.ModelForm):
    def clean_icon(self):
        return self._check_image('icon', max_file_size=1, types=['PNG'], max_size=(1000, 1000))

    class Meta:
        model = Benefit
        fields = '__all__'


class BenefitAdmin(admin.ModelAdmin):
    actions = ['delete_selected']
    form = BenefitForm

    def check_if_can_delete(self, request, queryset):
        linked_objs = BenefitLink.objects.filter(benefit__in=queryset).order_by('benefit__id')
        titles = {i.id: i.title for i in queryset}
        msg_parts = []
        for i, objs in groupby(linked_objs, lambda x: x.benefit_id):
            msg_part = [u'%s #%s' % (link.content_type.model_class()._meta.verbose_name, link.object_id)
                        for link in objs]
            msg_parts.append(u'%s: %s' % (titles[i], u', '.join(msg_part)))
        if msg_parts:
            msg = ungettext_lazy(
                u'Нельзя удалить выбранный объект, имеются связи: %s',
                u'Нельзя удалить выбранные объекты, имеются связи: %s',
                len(msg_parts)
            ) % u'; '.join(msg_parts)
            self.message_user(request, msg)
            return False
        return True

    def delete_view(self, request, object_id, extra_context=None):
        if not self.check_if_can_delete(request, self.model.objects.filter(id=quote(object_id))):
            opts = self.model._meta
            redirect_url = reverse(
                'admin:%s_%s_change' % (opts.app_label, opts.model_name),
                args=(quote(object_id),),
                current_app=self.admin_site.name
            )
            return HttpResponseRedirect(redirect_url)
        return super(BenefitAdmin, self).delete_view(request, object_id, extra_context)

    def delete_selected(self, request, queryset):
        if not self.check_if_can_delete(request, queryset):
            opts = self.model._meta
            redirect_url = reverse(
                'admin:%s_%s_changelist' % (opts.app_label, opts.model_name),
                current_app=self.admin_site.name
            )
            return HttpResponseRedirect(redirect_url)
        return delete_selected_original(self, request, queryset)
    delete_selected.short_description = delete_selected_original.short_description


class CoursePromotionAdmin(admin.ModelAdmin):
    list_display = ('sort', 'content_type', 'object_id', 'content_object')


admin.site.register(EducationalModule, EducationalModuleAdmin)
admin.site.register(EducationalModuleEnrollment, EducationalModuleEnrollmentAdmin)
admin.site.register(EducationalModuleEnrollmentType)
admin.site.register(EducationalModuleEnrollmentReason, EducationalModuleEnrollmentReasonAdmin)
admin.site.register(Benefit, BenefitAdmin)
admin.site.register(CoursePromotion, CoursePromotionAdmin)
