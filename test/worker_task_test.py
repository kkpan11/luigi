# -*- coding: utf-8 -*-
#
# Copyright 2012-2015 Spotify AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import multiprocessing
from subprocess import check_call
import sys

from helpers import LuigiTestCase, StringContaining
import mock
from psutil import Process
from time import sleep

import luigi
import luigi.date_interval
import luigi.notifications
from luigi.mock import MockTarget
from luigi.worker import TaskException, TaskProcess
from luigi.scheduler import DONE, FAILED

luigi.notifications.DEBUG = True


class WorkerTaskTest(LuigiTestCase):

    def test_constructor(self):
        class MyTask(luigi.Task):
            # Test overriding the constructor without calling the superconstructor
            # This is a simple mistake but caused an error that was very hard to understand

            def __init__(self):
                pass

        def f():
            luigi.build([MyTask()], local_scheduler=True)
        self.assertRaises(TaskException, f)

    def test_run_none(self):
        def f():
            luigi.build([None], local_scheduler=True)
        self.assertRaises(TaskException, f)


class TaskProcessTest(LuigiTestCase):

    def test_update_result_queue_on_success(self):
        # IMO this test makes no sense as it tests internal behavior and have
        # already broken once during internal non-changing refactoring
        class SuccessTask(luigi.Task):
            def on_success(self):
                return "test success expl"

        task = SuccessTask()
        result_queue = multiprocessing.Queue()
        task_process = TaskProcess(task, 1, result_queue, mock.Mock())

        with mock.patch.object(result_queue, 'put') as mock_put:
            task_process.run()
            mock_put.assert_called_once_with((task.task_id, DONE, "test success expl", [], None))

    def test_update_result_queue_on_failure(self):
        # IMO this test makes no sense as it tests internal behavior and have
        # already broken once during internal non-changing refactoring
        class FailTask(luigi.Task):
            def run(self):
                raise BaseException("Uh oh.")

            def on_failure(self, exception):
                return "test failure expl"

        task = FailTask()
        result_queue = multiprocessing.Queue()
        task_process = TaskProcess(task, 1, result_queue, mock.Mock())

        with mock.patch.object(result_queue, 'put') as mock_put:
            task_process.run()
            mock_put.assert_called_once_with((task.task_id, FAILED, "test failure expl", [], []))

    def test_fail_on_false_complete(self):
        class NeverCompleteTask(luigi.Task):
            def complete(self):
                return False

        task = NeverCompleteTask()
        result_queue = multiprocessing.Queue()
        task_process = TaskProcess(task, 1, result_queue, mock.Mock(), check_complete_on_run=True)

        with mock.patch.object(result_queue, 'put') as mock_put:
            task_process.run()
            mock_put.assert_called_once_with((
                task.task_id,
                FAILED,
                StringContaining("finished running, but complete() is still returning false"),
                [],
                None
            ))

    def test_fail_on_unfulfilled_dependencies(self):
        class NeverCompleteTask(luigi.Task):
            def complete(self):
                return False

        class A(NeverCompleteTask):
            def output(self):
                return []

        class B(NeverCompleteTask):
            def output(self):
                return MockTarget("foo-B")

        class C(NeverCompleteTask):
            def output(self):
                return [MockTarget("foo-C1"), MockTarget("foo-C2")]

        class Main(NeverCompleteTask):
            def requires(self):
                return [A(), B(), C()]

        task = Main()
        result_queue = multiprocessing.Queue()
        task_process = TaskProcess(task, 1, result_queue, mock.Mock())

        with mock.patch.object(result_queue, 'put') as mock_put:
            task_process.run()
            expected_missing = [A().task_id, f"{B().task_id} (foo-B)", f"{C().task_id} (foo-C1, foo-C2)"]
            mock_put.assert_called_once_with((
                task.task_id,
                FAILED,
                StringContaining(f"Unfulfilled dependencies at run time: {', '.join(expected_missing)}"),
                expected_missing,
                [],
            ))

    def test_cleanup_children_on_terminate(self):
        """
        Subprocesses spawned by tasks should be terminated on terminate
        """
        class HangingSubprocessTask(luigi.Task):
            def run(self):
                python = sys.executable
                check_call([python, '-c', 'while True: pass'])

        task = HangingSubprocessTask()
        queue = mock.Mock()
        worker_id = 1

        task_process = TaskProcess(task, worker_id, queue, mock.Mock())
        task_process.start()

        parent = Process(task_process.pid)
        while not parent.children():
            # wait for child process to startup
            sleep(0.01)

        [child] = parent.children()
        task_process.terminate()
        child.wait(timeout=1.0)  # wait for terminate to complete

        self.assertFalse(parent.is_running())
        self.assertFalse(child.is_running())

    def test_disable_worker_timeout(self):
        """
        When a task sets worker_timeout explicitly to 0, it should disable the timeout, even if it
        is configured globally.
        """
        class Task(luigi.Task):
            worker_timeout = 0

        task_process = TaskProcess(
            task=Task(),
            worker_id=1,
            result_queue=mock.Mock(),
            status_reporter=mock.Mock(),
            worker_timeout=10,

        )
        self.assertEqual(task_process.worker_timeout, 0)
