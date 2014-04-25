# -*- coding: utf-8 -*-

import logging
from launcher import app

from PyQt5.QtCore import QObject, pyqtSlot
from PyQt5.QtWidgets import QApplication

from collections import deque
import threading, os, sys
from multiprocessing.connection import Listener, Client
from urllib import parse

import constants
from mimeparser import UrlExtractor
import misc


class FrontendAction(object):
    def __repr__(self):
        return "FrontendAction, should be subclassed."

    def consume(self):
        raise NotImplementedError()


class CreateTasksAction(FrontendAction):
    _tasks = None  # tasks to add in the same batch

    def __init__(self, tasks):
        super().__init__()
        self._tasks = tasks

    def __repr__(self):
        return "{} {}".format(self.__class__.__name__, self._tasks)

    def consume(self):
        taskUrls = list(map(lambda task: task.url, self._tasks))
        if self._tasks[0].kind == CreateTask.NORMAL:
            app.frontendpy.sigCreateTasks.emit(taskUrls)
        else:
            app.mainWin.page.overrideFile = taskUrls[0]
            app.frontendpy.sigCreateTaskFromTorrentFile.emit()


class CreateTask(object):
    NORMAL = 0
    LOCAL_TORRENT = 1

    url = None
    kind = None

    def __init__(self, url = None, kind = None):
        self.url = url

        if kind is None:
            kind = self.NORMAL
        self.kind = kind

    def __repr__(self):
        return "{} <{}>".format(self.__class__.__name__, self.url)


class FrontendActionsQueue(QObject):
    _queue = None
    _listener = None
    _clipboard = None
    _urlExtractor = None

    def __init__(self, parent = None):
        super().__init__(parent)
        self._queue = deque()

        self._listener = threading.Thread(target = self.listenerThread, daemon = True,
                                          name = "frontend communication listener")
        self._listener.start()

        tasks = sys.argv[1:]
        if tasks:
            self.createTasksAction(tasks)

        self._urlExtractor = UrlExtractor(self)

        self._clipboard = QApplication.clipboard()
        app.settings.applySettings.connect(self.slotWatchClipboardToggled)

    def listenerThread(self):
        # clean if previous run crashes
        try:
            os.remove(constants.FRONTEND_SOCKET[0])
        except FileNotFoundError:
            pass

        with Listener(*constants.FRONTEND_SOCKET) as listener:
            while True:
                with listener.accept() as conn:
                    payload = conn.recv()
                    self.createTasksAction(payload)

    @pyqtSlot()
    def slotWatchClipboardToggled(self):
        try:
            self._clipboard.dataChanged.disconnect(self.slotClipboardDataChanged)
        except TypeError:
            pass  # not connected, meaning settings says no watch clipboard
        on = app.settings.getbool("frontend", "watchclipboard")
        if on:
            self._clipboard.dataChanged.connect(self.slotClipboardDataChanged)

    @pyqtSlot()
    def slotClipboardDataChanged(self):
        mimeData = self._clipboard.mimeData()
        self.createTasksFromMimeData(mimeData)

    def createTasksFromMimeData(self, data):
        # This method only checks text data.
        urls = self._urlExtractor.extract(data.text())
        if len(urls) > 0:
            self.createTasksAction(urls)

    def queueAction(self, action):
        self._queue.append(action)
        app.frontendpy.consumeAction("action newly queued")

    def dequeueAction(self):
        return self._queue.popleft()

    @pyqtSlot()
    @pyqtSlot(list)
    def createTasksAction(self, taskUrls = None):
        if taskUrls:
            alltasks = self._filterInvalidTasks(map(self._createTask, taskUrls))
            tasks = list(filter(lambda task: task.kind == CreateTask.NORMAL, alltasks))
            tasks_localtorrent = list(filter(lambda task: task.kind == CreateTask.LOCAL_TORRENT,
                                             alltasks))
        else:
            # else
            tasks = self._filterInvalidTasks([self._createTask()])
            tasks_localtorrent = []

        if tasks:
            self.queueAction(CreateTasksAction(tasks))
        for task_bt in tasks_localtorrent:  # because only 1 bt-task can be added once.
            self.queueAction(CreateTasksAction([task_bt]))

    @staticmethod
    def _filterInvalidTasks(tasks):
        # remove those urls which were not recognized by self._createTask
        return list(filter(lambda t: t is not None, tasks))

    @staticmethod
    def _createTask(taskUrl = None):
        if taskUrl is None:
            return CreateTask()

        if taskUrl.startswith("file://"):
            taskUrl = taskUrl[len("file://"):]

        parsed = parse.urlparse(taskUrl)
        if parsed.scheme in ("thunder", "flashget", "qqdl"):
            url = misc.decodePrivateLink(taskUrl)
            return CreateTask(url)

        elif parsed.scheme == "":
            if parsed.path.endswith(".torrent"):
                return CreateTask(taskUrl, kind = CreateTask.LOCAL_TORRENT)

        elif parsed.scheme in ("http", "https", "ftp", "magnet", "ed2k"):
            return CreateTask(taskUrl)


class FrontendCommunicationClient(object):
    def __init__(self, payload):
        with Client(*constants.FRONTEND_SOCKET) as conn:
            conn.send(payload)