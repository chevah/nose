# Copyright (c) 2011 Adi Roiban.
# See LICENSE for details.
'''TestCase for Twisted reactor and deferred.'''
from contextlib import contextmanager
from time import sleep
import inspect
import socket

from twisted.internet.posixbase import _SocketWaker, _UnixWaker, _SIGCHLDWaker
from twisted.python import threadable
from twisted.python.failure import Failure
# Workarround for twisted reactor restart.
threadable.registerAsIOThread()
from unittest2 import TestCase

# Import reactor last in case some other modules are changing the reactor.
from twisted.internet import reactor


class TwistedTestCase(TestCase):
    '''Test case for Twisted reactor and deferrers.'''

    EXCEPTED_DELAYED_CALLS = [
        '_resetLogDateTime',
        ]

    EXCEPTED_READERS = [
        _UnixWaker,
        _SocketWaker,
        _SIGCHLDWaker,
        ]

    def setUp(self):
        super(TwistedTestCase, self).setUp()
        self.reactor_timeout_failure = None

    @property
    def _caller_success_member(self):
        '''Retrieve the 'success' member from the test case.'''
        success = None
        for i in xrange(2, 6):
            try:
                success = inspect.stack()[i][0].f_locals['success']
                break
            except KeyError:
                success = None
        if success is None:
            raise AssertionError(u'Failed to find "success" attribute.')
        return success

    def tearDown(self):
        try:
            if self._caller_success_member:
                self.assertIsNone(self.reactor_timeout_failure)
                self.assertReactorIsClean()
            super(TwistedTestCase, self).tearDown()
        finally:
            self.cleanReactor()

    def _getDelayedString(self):
        '''Return a sting represenation of all delayed calles from reactor
        queue.'''
        delayed_string = []
        for delay in reactor.getDelayedCalls():
            delayed_string.append(str(delay))
        return '\n'.join(delayed_string)

    def runDeferred(self, deferred, timeout=1):
        '''Run the deferred in the reactor loop.

        Starts the reactor, waits for deferrerd execution,
        raises error in timeout, stops the reactor.
        '''
        if deferred.called is True:
            return

        def _stopReactor(result, timeout_call):
            '''Stop reactor and cancel timeout.'''
            timeout_call.cancel()
            if reactor.running:
                reactor.stop()
            return

        def _raiseTimeoutError():
            reactor.stop()
            raise AssertionError(
                u'Deferred took more than %.2f seconds to execute.' % timeout)

        timeout_call = reactor.callLater(timeout, _raiseTimeoutError)
        deferred.addCallback(_stopReactor, timeout_call)
        reactor.run()

    def _reactorQueueToString(self):
        result = []
        for delayed in reactor.getDelayedCalls():
            result.append(unicode(delayed.func))
        return '\n'.join(result)

    @classmethod
    def cleanReactor(cls):
        '''Remove all delayed calls and readers and writers.'''
        reactor.removeAll()
        reactor.threadCallQueue = []
        for delayed_call in reactor.getDelayedCalls():
            if delayed_call.active():
                delayed_call.cancel()

    def assertReactorIsClean(self):
        '''Check that the reactor has no delayed calls, readers or writers.

        It also cleans the reactor on errors.
        '''

        def raise_failure(location, reason):
            raise AssertionError(
                'Reactor is not clean. %s %s' % (location, reason))

        # Look at threads queue.
        if len(reactor.threadCallQueue) > 0:
            raise_failure('threads', str(reactor.threadCallQueue))

        if len(reactor.getWriters()) > 0:
            raise_failure('writers', str(reactor.getWriters()))

        for reader in reactor.getReaders():
            excepted = False
            for reader_type in self.EXCEPTED_READERS:
                if isinstance(reader, reader_type):
                    excepted = True
                    break
            if not excepted:
                raise_failure('readers', str(reactor.getReaders()))

        for delayed_call in reactor.getDelayedCalls():
            if delayed_call.active():
                delayed_str = unicode(delayed_call.func).split()[1]
                if delayed_str in self.EXCEPTED_DELAYED_CALLS:
                    continue
                raise_failure('', 'Active delayed calls: %s' % delayed_call)

    def executeReactor(self, timeout=1, debug=False, run_once=False,
                       have_thread=False):
        '''Run reactor for timeout until no more delayedcalls, readers or
        writers or threads are in the queues.
        '''
        self.timeout_reached = False

        def raise_timeout_error():
            self.timeout_reached = True
            failure = AssertionError(
                u'Reactor took more than %.2f seconds to execute.' % timeout)
            self.reactor_timeout_failure = failure

        # Set up timeout.
        timeout_call = reactor.callLater(timeout, raise_timeout_error)

        # Fake a reactor start/restart.
        reactor.fireSystemEvent('startup')

        if have_thread:
            # Thread are always hard to sync, and this is why we need to
            # sleep for a few second so that the operating system will
            # call the thread and allow it to sync its state with the main
            # reactor.
            sleep(0.11)

        # Set it to True to enter the first loop.
        have_callbacks = True
        while have_callbacks and not self.timeout_reached:
            if debug:
                '''When debug is enabled with iterate using 1 second steps,
                to have a much better debug output.
                Otherwise the debug messages will flood the output.'''
                print (
                    u'delayed: %s\n'
                    u'threads: %s\n'
                    u'writers: %s\nreaders:%s\n\n' % (
                        self._getDelayedString(),
                        reactor.threadCallQueue,
                        reactor.getWriters(),
                        reactor.getReaders(),
                        ))
                reactor.iterate(1)
            else:
                reactor.iterate()

            have_callbacks = False

            # Look at delayed calls.
            for delayed in reactor.getDelayedCalls():
                # We skip our own timeout call.
                if delayed is timeout_call:
                    continue
                delayed_str = unicode(delayed.func)
                is_exception = False
                for excepted_callback in self.EXCEPTED_DELAYED_CALLS:
                    if excepted_callback in delayed_str:
                        is_exception = True
                if not is_exception:
                    have_callbacks = True
                    continue

            if run_once:
                if have_callbacks:
                    raise AssertionError(
                        u'Reactor queue still contains delayed deferred.\n'
                        u'%s' % (self._reactorQueueToString()))
                break

            # Look at writters buffers:
            if len(reactor.getWriters()) > 0:
                have_callbacks = True
                continue

            for reader in reactor.getReaders():
                have_callbacks = True
                for excepted_reader in self.EXCEPTED_READERS:
                    if isinstance(reader, excepted_reader):
                        have_callbacks = False
                        break
                if have_callbacks:
                    break

            # Look at threads queue.
            if len(reactor.threadCallQueue) > 0:
                have_callbacks = True
                continue

        if not self.timeout_reached:
            # Everything fine, disable timeout.
            if not timeout_call.cancelled:
                timeout_call.cancel()

    @contextmanager
    def listenPort(self, ip, port):
        '''Context manager for binding a port.'''
        test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_socket.bind((ip, port))
        test_socket.listen(0)
        yield
        try:
            test_socket.shutdown(2)
        except socket.error, error:
            if 'solaris-10' in self.getHostname() and error.args[0] == 134:
                pass
            else:
                raise

    def assertIsListening(self, ip, port, debug=False):
        '''Check if the port and address are in listening mode.'''
        test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_socket.settimeout(1.0)
        try:
            test_socket.connect((ip, port))
            sock_name = test_socket.getsockname()
            test_socket.shutdown(2)
            if debug:
                print 'Connected as: %s:%d' % (sock_name[0], sock_name[1])
        except:
            raise AssertionError(
                u'It seems that no one is listening on %s:%d' % (
                    ip, port))

    def assertIsNotListening(self, ip, port):
        '''Check if the port and address are in listening mode.'''
        try:
            self.assertIsListening(ip, port)
        except AssertionError:
            return
        raise AssertionError(
            u'It seems that someone is listening on %s:%d' % (
                ip, port))

    def assertIsFailure(self, deferred):
        if not isinstance(deferred.result, Failure):
            raise AssertionError(u'Deferred is not a failure.')

    def assertWasCalled(self, deferred):
        if not deferred.called:
            raise AssertionError('This deferred was not called yet.')

    def assertIsNotFailure(self, deferred):
        '''Raise assertion error if deferred is a Failure.'''
        if not hasattr(deferred, 'result'):
            raise AssertionError('This deferred was not called yet.')

        if isinstance(deferred.result, Failure):
            raise AssertionError(
                u'Deferred %s contains a failure' % (deferred))

    def assertFailureType(self, failure_class, deferred):
        '''Raise assertion error if failure is not of required type.'''
        self.assertIsFailure(deferred)

        if deferred.result.type is not failure_class:
            raise AssertionError(
                u'Failure for %s is not of type %s' % (
                    deferred, failure_class))

    def assertFailureID(self, failure_id, deferred):
        '''Raise assertion error if failure does not have the required id.'''
        self.assertIsFailure(deferred)

        failure_value = deferred.result.value
        if failure_value.id != failure_id:
            raise AssertionError(
                u'Failure id for %s is not %d, but %d' % (
                    deferred, failure_id, failure_value.id))

    def ignoreFailure(self, deferred):
        '''Ignore the current failure on the deferred.'''
        deferred.addErrback(lambda failure: None)

    def getHostname(self):
        return socket.gethostname()
