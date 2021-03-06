# Copyright (C) 2011-2013 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
#
# See the License for the specific language governing permissions and
# limitations under the License.

import abc
import sys
import threading
import time
import types

import keystoneauth1.exceptions
import simplejson
import six

from shade import _log
from shade import meta


def _is_listlike(obj):
    # NOTE(Shrews): Since the client API might decide to subclass one
    # of these result types, we use isinstance() here instead of type().
    return (
        isinstance(obj, list) or
        isinstance(obj, types.GeneratorType))


def _is_objlike(obj):
    # NOTE(Shrews): Since the client API might decide to subclass one
    # of these result types, we use isinstance() here instead of type().
    return (
        not isinstance(obj, bool) and
        not isinstance(obj, int) and
        not isinstance(obj, float) and
        not isinstance(obj, six.string_types) and
        not isinstance(obj, set) and
        not isinstance(obj, tuple))


@six.add_metaclass(abc.ABCMeta)
class BaseTask(object):
    """Represent a task to be performed on an OpenStack Cloud.

    Some consumers need to inject things like rate-limiting or auditing
    around each external REST interaction. Task provides an interface
    to encapsulate each such interaction. Also, although shade itself
    operates normally in a single-threaded direct action manner, consuming
    programs may provide a multi-threaded TaskManager themselves. For that
    reason, Task uses threading events to ensure appropriate wait conditions.
    These should be a no-op in single-threaded applications.

    A consumer is expected to overload the main method.

    :param dict kw: Any args that are expected to be passed to something in
                    the main payload at execution time.
    """

    def __init__(self, **kw):
        self._exception = None
        self._traceback = None
        self._result = None
        self._response = None
        self._finished = threading.Event()
        self.args = kw
        self.name = type(self).__name__

    @abc.abstractmethod
    def main(self, client):
        """ Override this method with the actual workload to be performed """

    def done(self, result):
        self._result = result
        self._finished.set()

    def exception(self, e, tb):
        self._exception = e
        self._traceback = tb
        self._finished.set()

    def wait(self, raw=False):
        self._finished.wait()

        if self._exception:
            six.reraise(type(self._exception), self._exception,
                        self._traceback)

        return self._result

    def run(self, client):
        self._client = client
        try:
            # Retry one time if we get a retriable connection failure
            try:
                self.done(self.main(client))
            except keystoneauth1.exceptions.RetriableConnectionFailure:
                client.log.debug(
                    "Connection failure for {name}, retrying".format(
                        name=type(self).__name__))
                self.done(self.main(client))
            except Exception:
                raise
        except Exception as e:
            self.exception(e, sys.exc_info()[2])


class Task(BaseTask):
    """ Shade specific additions to the BaseTask Interface. """

    def wait(self, raw=False):
        super(Task, self).wait()

        if raw:
            # Do NOT convert the result.
            return self._result

        if _is_listlike(self._result):
            return meta.obj_list_to_dict(self._result)
        elif _is_objlike(self._result):
            return meta.obj_to_dict(self._result)
        else:
            return self._result


class RequestTask(BaseTask):
    """ Extensions to the Shade Tasks to handle raw requests """

    # It's totally legit for calls to not return things
    result_key = None

    # keystoneauth1 throws keystoneauth1.exceptions.http.HttpError on !200
    def done(self, result):
        self._response = result

        try:
            result_json = self._response.json()
        except (simplejson.scanner.JSONDecodeError, ValueError) as e:
            result_json = self._response.text
            self._client.log.debug(
                'Could not decode json in response: {e}'.format(e=str(e)))
            self._client.log.debug(result_json)

        if self.result_key:
            self._result = result_json[self.result_key]
        else:
            self._result = result_json

        self._request_id = self._response.headers.get('x-openstack-request-id')
        self._finished.set()

    def wait(self, raw=False):
        super(RequestTask, self).wait()

        if raw:
            # Do NOT convert the result.
            return self._result

        if _is_listlike(self._result):
            return meta.obj_list_to_dict(
                self._result, request_id=self._request_id)
        elif _is_objlike(self._result):
            return meta.obj_to_dict(self._result, request_id=self._request_id)
        return self._result


def _result_filter_cb(result):
    return result


def generate_task_class(method, name, result_filter_cb):
    if name is None:
        if callable(method):
            name = method.__name__
        else:
            name = method

    class RunTask(Task):
        def __init__(self, **kw):
            super(RunTask, self).__init__(**kw)
            self.name = name
            self._method = method

        def wait(self, raw=False):
            super(RunTask, self).wait()

            if raw:
                # Do NOT convert the result.
                return self._result
            return result_filter_cb(self._result)

        def main(self, client):
            if callable(self._method):
                return method(**self.args)
            else:
                meth = getattr(client, self._method)
                return meth(**self.args)
    return RunTask


class TaskManager(object):
    log = _log.setup_logging(__name__)

    def __init__(self, client, name, result_filter_cb=None):
        self.name = name
        self._client = client
        if not result_filter_cb:
            self._result_filter_cb = _result_filter_cb
        else:
            self._result_filter_cb = result_filter_cb

    def stop(self):
        """ This is a direct action passthrough TaskManager """
        pass

    def run(self):
        """ This is a direct action passthrough TaskManager """
        pass

    def submit_task(self, task, raw=False):
        """Submit and execute the given task.

        :param task: The task to execute.
        :param bool raw: If True, return the raw result as received from the
            underlying client call.
        """
        self.log.debug(
            "Manager %s running task %s" % (self.name, task.name))
        start = time.time()
        task.run(self._client)
        end = time.time()
        self.log.debug(
            "Manager %s ran task %s in %ss" % (
                self.name, task.name, (end - start)))
        return task.wait(raw)
    # Backwards compatibility
    submitTask = submit_task

    def submit_function(
            self, method, name=None, result_filter_cb=None, **kwargs):
        """ Allows submitting an arbitrary method for work.

        :param method: Method to run in the TaskManager. Can be either the
                       name of a method to find on self.client, or a callable.
        """
        if not result_filter_cb:
            result_filter_cb = self._result_filter_cb

        task_class = generate_task_class(method, name, result_filter_cb)

        return self.manager.submit_task(task_class(**kwargs))
