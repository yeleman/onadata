import math

from celery import task
from datetime import datetime

from django.contrib.gis.db import models
from django.db import connection
from django.db import transaction
from django.db.models.signals import post_save
from django.db.models.signals import post_delete
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.gis.geos import GeometryCollection, Point
from django.contrib.postgres.fields import JSONField
from django.core.urlresolvers import reverse
from django.utils import timezone
from django.utils.translation import ugettext as _
from taggit.managers import TaggableManager

from onadata.apps.logger.models.survey_type import SurveyType
from onadata.apps.logger.models.xform import XForm
from onadata.apps.logger.models.xform import XFORM_TITLE_LENGTH
from onadata.apps.logger.xform_instance_parser import XFormInstanceParser,\
    clean_and_parse_xml, get_uuid_from_xml
from onadata.libs.utils.common_tags import ATTACHMENTS, BAMBOO_DATASET_ID,\
    DELETEDAT, EDITED, GEOLOCATION, ID, MONGO_STRFTIME, NOTES, \
    SUBMISSION_TIME, TAGS, UUID, XFORM_ID_STRING, SUBMITTED_BY, VERSION, \
    STATUS, DURATION, START, END, LAST_EDITED
from onadata.libs.utils.model_tools import set_uuid
from onadata.libs.data.query import get_numeric_fields
from onadata.libs.utils.cache_tools import safe_delete
from onadata.libs.utils.cache_tools import IS_ORG
from onadata.libs.utils.cache_tools import PROJ_SUB_DATE_CACHE
from onadata.libs.utils.cache_tools import PROJ_NUM_DATASET_CACHE,\
    XFORM_DATA_VERSIONS, DATAVIEW_COUNT
from onadata.libs.utils.dict_tools import get_values_matching_key
from onadata.libs.utils.timing import calculate_duration

ASYNC_POST_SUBMISSION_PROCESSING_ENABLED = \
    getattr(settings, 'ASYNC_POST_SUBMISSION_PROCESSING_ENABLED', False)


def get_attachment_url(attachment, suffix=None):
    kwargs = {'pk': attachment.pk}
    url = u'{}?filename={}'.format(
        reverse('files-detail', kwargs=kwargs),
        attachment.media_file.name
    )
    if suffix:
        url += u'&suffix={}'.format(suffix)

    return url


def _get_attachments_from_instance(instance):
    attachments = []
    for a in instance.attachments.all():
        attachment = dict()
        attachment['download_url'] = get_attachment_url(a)
        attachment['small_download_url'] = get_attachment_url(a, 'small')
        attachment['medium_download_url'] = get_attachment_url(a, 'medium')
        attachment['mimetype'] = a.mimetype
        attachment['filename'] = a.media_file.name
        attachment['instance'] = a.instance.pk
        attachment['xform'] = instance.xform.id
        attachment['id'] = a.id
        attachments.append(attachment)

    return attachments


def _get_tag_or_element_type_xpath(xform, tag):
    elems = xform.get_survey_elements_of_type(tag)

    return elems[0].get_abbreviated_xpath() if elems else tag


class FormInactiveError(Exception):

    def __unicode__(self):
        return _("Form is inactive")

    def __str__(self):
        return unicode(self).encode('utf-8')


def numeric_checker(string_value):
    if string_value.isdigit():
        return int(string_value)
    else:
        try:
            value = float(string_value)
            if math.isnan(value):
                value = 0
            return value
        except ValueError:
            pass


# need to establish id_string of the xform before we run get_dict since
# we now rely on data dictionary to parse the xml


def get_id_string_from_xml_str(xml_str):
    xml_obj = clean_and_parse_xml(xml_str)
    root_node = xml_obj.documentElement
    id_string = root_node.getAttribute(u"id")

    if len(id_string) == 0:
        # may be hidden in submission/data/id_string
        elems = root_node.getElementsByTagName('data')

        for data in elems:
            for child in data.childNodes:
                id_string = data.childNodes[0].getAttribute('id')

                if len(id_string) > 0:
                    break

            if len(id_string) > 0:
                break

    return id_string


def submission_time():
    return timezone.now()


@task
@transaction.atomic()
def update_xform_submission_count(instance_id, created):
    if created:
        try:
            instance = Instance.objects.select_related(
                'xform__user__id'
            ).get(pk=instance_id)
        except Instance.DoesNotExist:
            pass
        else:
            # update xform.num_of_submissions
            cursor = connection.cursor()
            sql = (
                'UPDATE logger_xform SET '
                'num_of_submissions = num_of_submissions + 1, '
                'last_submission_time = %s '
                'WHERE id = %s'
            )
            params = [instance.date_created, instance.xform_id]

            # update user profile.num_of_submissions
            cursor.execute(sql, params)
            sql = (
                'UPDATE main_userprofile SET '
                'num_of_submissions = num_of_submissions + 1 '
                'WHERE user_id = %s'
            )
            cursor.execute(sql, [instance.xform.user_id])

            safe_delete('{}{}'.format(XFORM_DATA_VERSIONS, instance.xform_id))
            safe_delete('{}{}'.format(DATAVIEW_COUNT, instance.xform_id))


def update_xform_submission_count_delete(sender, instance, **kwargs):
    try:
        xform = XForm.objects.select_for_update().get(pk=instance.xform.pk)
    except XForm.DoesNotExist:
        pass
    else:
        xform.num_of_submissions -= 1
        if xform.num_of_submissions < 0:
            xform.num_of_submissions = 0
        xform.save(update_fields=['num_of_submissions'])
        profile_qs = User.profile.get_queryset()
        try:
            profile = profile_qs.select_for_update()\
                .get(pk=xform.user.profile.pk)
        except profile_qs.model.DoesNotExist:
            pass
        else:
            profile.num_of_submissions -= 1
            if profile.num_of_submissions < 0:
                profile.num_of_submissions = 0
            profile.save()

        for a in [PROJ_NUM_DATASET_CACHE, PROJ_SUB_DATE_CACHE]:
            safe_delete('{}{}'.format(a, xform.project.pk))

        safe_delete('{}{}'.format(IS_ORG, xform.pk))
        safe_delete('{}{}'.format(XFORM_DATA_VERSIONS, xform.pk))
        safe_delete('{}{}'.format(DATAVIEW_COUNT, xform.pk))

        if xform.instances.exclude(geom=None).count() < 1:
            xform.instances_with_geopoints = False
            xform.save()


@task
def save_full_json(instance_id, created):
    """set json data, ensure the primary key is part of the json data"""
    if created:
        try:
            instance = Instance.objects.get(pk=instance_id)
        except Instance.DoesNotExist:
            pass
        else:
            instance.json = instance.get_full_dict()
            instance.save(update_fields=['json'])


@task
def update_project_date_modified(instance_id, created):
    # update the date modified field of the project which will change
    # the etag value of the projects endpoint
    try:
        instance = Instance.objects.select_related(
            'xform__project__date_modified'
        ).get(pk=instance_id)
    except Instance.DoesNotExist:
        pass
    else:
        instance.xform.project.save(update_fields=['date_modified'])


def convert_to_serializable_date(date):
    if hasattr(date, 'isoformat'):
        return date.isoformat()

    return date


class InstanceBaseClass(object):
    """Interface of functions for Instance and InstanceHistory model"""

    @property
    def point(self):
        gc = self.geom

        if gc and len(gc):
            return gc[0]

    def numeric_converter(self, json_dict, numeric_fields=None):
        if numeric_fields is None:
            numeric_fields = get_numeric_fields(self.xform)
        for key, value in json_dict.items():
            if isinstance(value, basestring) and key in numeric_fields:
                converted_value = numeric_checker(value)
                if converted_value:
                    json_dict[key] = converted_value
            elif isinstance(value, dict):
                json_dict[key] = self.numeric_converter(
                    value, numeric_fields)
            elif isinstance(value, list):
                for k, v in enumerate(value):
                    if isinstance(v, basestring) and key in numeric_fields:
                        converted_value = numeric_checker(v)
                        if converted_value:
                            json_dict[key] = converted_value
                    elif isinstance(v, dict):
                        value[k] = self.numeric_converter(
                            v, numeric_fields)
        return json_dict

    def _set_geom(self):
        xform = self.xform
        geo_xpaths = xform.geopoint_xpaths()
        doc = self.get_dict()
        points = []

        if len(geo_xpaths):
            for xpath in geo_xpaths:
                for gps in get_values_matching_key(doc, xpath):
                    try:
                        geometry = [float(s) for s in gps.split()]
                        lat, lng = geometry[0:2]
                        points.append(Point(lng, lat))
                    except ValueError:
                        return

            if not xform.instances_with_geopoints and len(points):
                xform.instances_with_geopoints = True
                xform.save()

            self.geom = GeometryCollection(points)

    def _set_json(self):
        self.json = self.get_full_dict()

    def get_full_dict(self, load_existing=True):
        doc = self.json or {} if load_existing else {}
        # Get latest dict
        doc = self.get_dict()

        if self.id:
            doc.update({
                UUID: self.uuid,
                ID: self.id,
                BAMBOO_DATASET_ID: self.xform.bamboo_dataset,
                ATTACHMENTS: _get_attachments_from_instance(self),
                STATUS: self.status,
                TAGS: list(self.tags.names()),
                NOTES: self.get_notes(),
                VERSION: self.version,
                DURATION: self.get_duration(),
                XFORM_ID_STRING: self._parser.get_xform_id_string(),
                GEOLOCATION: [self.point.y, self.point.x] if self.point
                else [None, None],
                SUBMITTED_BY: self.user.username if self.user else None
            })

            for osm in self.osm_data.all():
                doc.update(osm.get_tags_with_prefix())

            if isinstance(self.deleted_at, datetime):
                doc[DELETEDAT] = self.deleted_at.strftime(MONGO_STRFTIME)

            if not self.date_created:
                self.date_created = submission_time()

            doc[SUBMISSION_TIME] = self.date_created.strftime(MONGO_STRFTIME)

            edited = False
            if hasattr(self, 'last_edited'):
                edited = self.last_edited is not None

            doc[EDITED] = edited
            edited and doc.update({
                LAST_EDITED: convert_to_serializable_date(self.last_edited)
            })

        return doc

    def _set_parser(self):
        if not hasattr(self, "_parser"):
            self._parser = XFormInstanceParser(self.xml, self.xform)

    def _set_survey_type(self):
        self.survey_type, created = \
            SurveyType.objects.get_or_create(slug=self.get_root_node_name())

    def _set_uuid(self):
        if self.xml and not self.uuid:
            uuid = get_uuid_from_xml(self.xml)
            if uuid is not None:
                self.uuid = uuid
        set_uuid(self)

    def get(self, abbreviated_xpath):
        self._set_parser()
        return self._parser.get(abbreviated_xpath)

    def get_dict(self, force_new=False, flat=True):
        """Return a python object representation of this instance's XML."""
        self._set_parser()

        instance_dict = self._parser.get_flat_dict_with_attributes() if flat \
            else self._parser.to_dict()
        return self.numeric_converter(instance_dict)

    def get_notes(self):
        return [{"id": note.id,
                 "owner": note.created_by.username,
                 "note": note.note,
                 "instance_field": note.instance_field,
                 "created_by": note.created_by.id}
                for note in self.notes.all()]

    def get_root_node(self):
        self._set_parser()
        return self._parser.get_root_node()

    def get_root_node_name(self):
        self._set_parser()
        return self._parser.get_root_node_name()

    def get_duration(self):
        data = self.get_dict()
        start_name = _get_tag_or_element_type_xpath(self.xform, START)
        end_name = _get_tag_or_element_type_xpath(self.xform, END)
        start_time, end_time = data.get(start_name), data.get(end_name)

        return calculate_duration(start_time, end_time)


class Instance(models.Model, InstanceBaseClass):
    """
    Model representing a single submission to an XForm
    """

    json = JSONField(default=dict, null=False)
    xml = models.TextField()
    user = models.ForeignKey(User, related_name='instances', null=True)
    xform = models.ForeignKey(XForm, null=False, related_name='instances')
    survey_type = models.ForeignKey(SurveyType)

    # shows when we first received this instance
    date_created = models.DateTimeField(auto_now_add=True)

    # this will end up representing "date last parsed"
    date_modified = models.DateTimeField(auto_now=True)

    # this will end up representing "date instance was deleted"
    deleted_at = models.DateTimeField(null=True, default=None)

    # this will be edited when we need to create a new InstanceHistory object
    last_edited = models.DateTimeField(null=True, default=None)

    # ODK keeps track of three statuses for an instance:
    # incomplete, submitted, complete
    # we add a fourth status: submitted_via_web
    status = models.CharField(max_length=20,
                              default=u'submitted_via_web')
    uuid = models.CharField(max_length=249, default=u'')
    version = models.CharField(max_length=XFORM_TITLE_LENGTH, null=True)

    # store a geographic objects associated with this instance
    geom = models.GeometryCollectionField(null=True)

    tags = TaggableManager()

    class Meta:
        app_label = 'logger'
        unique_together = ('xform', 'uuid')

    @classmethod
    def set_deleted_at(cls, instance_id, deleted_at=timezone.now()):
        try:
            instance = cls.objects.get(id=instance_id)
        except cls.DoesNotExist:
            pass
        else:
            instance.set_deleted(deleted_at)

    def _check_active(self, force):
        """Check that form is active and raise exception if not.

        :param force: Ignore restrictions on saving.
        """
        if not force and self.xform and not self.xform.downloadable:
            raise FormInactiveError()

    def save(self, *args, **kwargs):
        force = kwargs.get('force')

        if force:
            del kwargs['force']

        self._check_active(force)

        self._set_geom()
        self._set_json()
        self._set_survey_type()
        self._set_uuid()
        self.version = self.xform.version
        super(Instance, self).save(*args, **kwargs)

    def set_deleted(self, deleted_at=timezone.now(), force=False):
        self.deleted_at = deleted_at
        self.save(force=force)
        # force submission count re-calculation
        self.xform.submission_count(force_update=True)
        self.parsed_instance.save()


def post_save_submission(sender, instance=None, created=False, **kwargs):
    if ASYNC_POST_SUBMISSION_PROCESSING_ENABLED:
        update_xform_submission_count.apply_async(args=[instance.pk, created])
        save_full_json.apply_async(args=[instance.pk, created])
        update_project_date_modified.apply_async(args=[instance.pk, created])
    else:
        update_xform_submission_count(instance.pk, created)
        save_full_json(instance.pk, created)
        update_project_date_modified(instance.pk, created)


post_save.connect(post_save_submission, sender=Instance,
                  dispatch_uid='post_save_submission')

post_delete.connect(update_xform_submission_count_delete, sender=Instance,
                    dispatch_uid='update_xform_submission_count_delete')


class InstanceHistory(models.Model, InstanceBaseClass):

    class Meta:
        app_label = 'logger'

    xform_instance = models.ForeignKey(
        Instance, related_name='submission_history')
    user = models.ForeignKey(User, null=True)

    xml = models.TextField()
    # old instance id
    uuid = models.CharField(max_length=249, default=u'')

    date_created = models.DateTimeField(auto_now_add=True)
    date_modified = models.DateTimeField(auto_now=True)
    submission_date = models.DateTimeField(null=True, default=None)
    geom = models.GeometryCollectionField(null=True)
    objects = models.GeoManager()

    @property
    def xform(self):
        return self.xform_instance.xform

    @property
    def attachments(self):
        return self.xform_instance.attachments.all()

    @property
    def json(self):
        return self.get_full_dict(load_existing=False)

    @property
    def status(self):
        return self.xform_instance.status

    @property
    def tags(self):
        return self.xform_instance.tags

    @property
    def notes(self):
        return self.xform_instance.notes.all()

    @property
    def version(self):
        return self.xform_instance.version

    @property
    def osm_data(self):
        return self.xform_instance.osm_data

    @property
    def deleted_at(self):
        return None

    def _set_parser(self):
        if not hasattr(self, "_parser"):
            self._parser = XFormInstanceParser(
                self.xml, self.xform_instance.xform
            )

    @classmethod
    def set_deleted_at(cls, instance_id, deleted_at=timezone.now()):
        return None
