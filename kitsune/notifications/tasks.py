import logging

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

import actstream.registry
import requests
from actstream.models import Action, Follow
from celery import task
from requests.exceptions import RequestException

from kitsune.notifications.models import (
    Notification, RealtimeRegistration, PushNotificationRegistration)
from kitsune.notifications.decorators import notification_handler, notification_handlers


logger = logging.getLogger('k.notifications.tasks')


def _ct_query(object, actor_only=None, **kwargs):
    ct = ContentType.objects.get_for_model(object)
    if actor_only is not None:
        kwargs['actor_only'] = actor_only
    return Q(content_type=ct.pk, object_id=object.pk, **kwargs)


def _full_ct_query(action, actor_only=None):
    """Build a query that matches objects with a content type that matches an action."""
    actstream.registry.check(action.actor)
    query = _ct_query(action.actor)
    if action.target is not None:
        actstream.registry.check(action.target)
        query |= _ct_query(action.target, actor_only)
    if action.action_object is not None:
        actstream.registry.check(action.action_object)
        query |= _ct_query(action.action_object, actor_only)
    return query


def _send_simple_push(endpoint, version):
    """
    Hit a simple push endpoint to send a notification to a user.

    Handles and record any HTTP errors.
    """
    try:
        r = requests.put(endpoint, 'version={}'.format(version))
        # If something does wrong, the SimplePush server will give back
        # json encoded error messages.
        if r.status_code != 200:
            logger.error('SimplePush error: %s %s', r.status_code, r.json())
    except RequestException as e:
        # This will go to Sentry.
        logger.error('SimplePush PUT failed: %s', e)


@task(ignore_result=True)
def add_notification_for_action(action_id):
    action = Action.objects.get(id=action_id)
    query = _full_ct_query(action, actor_only=False)
    # Don't send notifications to a user about actions they take.
    query &= ~Q(user=action.actor)

    # execute the above query, iterate through the results, get every user
    # assocated with those Follow objects, and fire off a notification to
    # every one of them. Use a set to only notify each user once.
    users_to_notify = set(f.user for f in Follow.objects.filter(query))
    # Don't use bulk save since that wouldn't trigger signal handlers
    for u in users_to_notify:
        Notification.objects.create(owner=u, action=action)


@task(ignore_result=True)
def send_realtimes_for_action(action_id):
    action = Action.objects.get(id=action_id)
    query = _full_ct_query(action)
    # Don't send notifications to a user about actions they take.
    query &= ~Q(creator=action.actor)

    registrations = RealtimeRegistration.objects.filter(query)
    for reg in registrations:
        _send_simple_push(reg.endpoint, action.id)


@task(ignore_result=True)
def send_notification(notification_id):
    """Call every notification handler for a notification."""
    notification = Notification.objects.get(id=notification_id)
    for handler in notification_handlers:
        handler(notification)


@notification_handler
def simple_push(notification):
    """
    Send simple push notifications to users that have opted in to them.

    This will be called as a part of a celery task.
    """
    registrations = PushNotificationRegistration.objects.filter(creator=notification.owner)
    for reg in registrations:
        _send_simple_push(reg.push_url, notification.id)
