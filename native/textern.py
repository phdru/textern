#!/usr/bin/env python3
# vim: set et ts=4 tw=80:
# This file is part of Textern.
# Copyright (C) 2017-2018  Jonathan Lebon <jonathan@jlebon.com>
# Copyright (C) 2018  Oleg Broytman <phd@phdru.name>
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import print_function
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import time

try:
    from inotify_simple import INotify, flags
except ImportError:
    # fall-back to the old import pattern for git-submodule use-case (PR#57)
    from inotify_simple.inotify_simple import INotify, flags


BACKUP_RETENTION_SECS = 24 * 60 * 60


class TmpManager():

    def __init__(self):
        try:
            tmpdir_parent = os.path.join(
                os.environ['XDG_RUNTIME_DIR'], 'textern')
            os.makedirs(tmpdir_parent)
        except FileExistsError:
            pass
        except (KeyError, OSError):
            tmpdir_parent = None
        self._backupdir = ""
        self.tmpdir = tempfile.mkdtemp(prefix="textern-", dir=tmpdir_parent)
        self._tmpfiles = {}  # relfn --> opaque
        self._editors = {}  # absfn --> proc
        self.kill_editors_allow = False
        self.kill_editors_timeout = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        shutil.rmtree(self.tmpdir)

    def __bool__(self, relfn):
        return bool(self._tmpfiles)

    __nonzero__ = __bool__  # Python 2.7

    def __contains__(self, relfn):
        return relfn in self._tmpfiles

    def update_backupdir(self, val):
        self._backupdir = val
        if self._backupdir != "":
            os.makedirs(self._backupdir, exist_ok=True)
            now = time.time()
            for path in os.listdir(self._backupdir):
                fn = os.path.join(self._backupdir, path)
                if not os.path.isfile(fn):
                    continue
                stbuf = os.stat(fn)
                if now - stbuf.st_mtime > BACKUP_RETENTION_SECS:
                    os.unlink(fn)

    def new(self, text, url, extension, opaque):
        sanitized_url = urllib.parse.quote(url, safe='')
        f, absfn = tempfile.mkstemp(dir=self.tmpdir,
                                    prefix=(sanitized_url + '-'),
                                    suffix=("." + extension))
        # this itself will cause a harmless inotify event, though as a cool
        # side effect, we get an initial highlighting of the text area which is
        # nice feedback that the command was received
        os.write(f, text.encode("utf-8"))
        os.close(f)
        relfn = os.path.basename(absfn)
        assert relfn not in self._tmpfiles
        self._tmpfiles[relfn] = opaque
        return absfn

    def delete(self, absfn):
        relfn = os.path.basename(absfn)
        assert relfn in self._tmpfiles
        self._tmpfiles.pop(relfn)
        os.unlink(absfn)
        del self._editors[absfn]

    def backup(self, relfn):
        if self._backupdir == "":
            return
        assert relfn in self._tmpfiles
        absfn = os.path.join(self.tmpdir, relfn)
        if os.stat(absfn).st_size > 0:
            shutil.copyfile(absfn, os.path.join(self._backupdir, relfn))

    def get(self, relfn):
        assert relfn in self._tmpfiles
        with open(os.path.join(self.tmpdir, relfn), encoding='utf-8') as f:
            return f.read(), self._tmpfiles[relfn]

    def add_editor(self, absfn, editor, id, proc):
        relfn = os.path.basename(absfn)
        assert relfn in self._tmpfiles
        self._editors[absfn] = (editor, id, proc)

    def poll(self):
        for absfn, editor, id, proc in list(self._editors.items()):
            if proc.poll() is None:  # still running:
                continue
            if proc.returncode != 0:
                send_error("editor '%s' did not exit successfully"
                           % editor)
            send_death_notice(id)
            self.delete(absfn)

    def kill_editors_configure(self, allow, timeout):
        self.kill_editors_allow = allow
        self.kill_editors_timeout = timeout

    def kill_editors(self):
        """Terminate editors that aren't finished yet"""
        if not self.kill_editors_allow or not self._editors:
            return
        for proc in self._editors.values():
            proc.terminate()
        time.sleep(self.kill_editors_timeout)
        # Try harder
        for proc in self._editors.values():
            proc.kill()


def main():
    with INotify() as ino, TmpManager() as tmp_mgr:
        ino.add_watch(tmp_mgr.tmpdir, flags.CLOSE_WRITE)

        thread1 = threading.Thread(target=handle_stdin, args=(tmp_mgr,))
        thread1.start()

        thread2 = threading.Thread(target=handle_inotify_event,
                                   args=(ino, tmp_mgr))
        thread2.start()

        sys.stdin.close()
        thread1.join()
        ino.close()
        thread2.join()
        tmp_mgr.kill_editors()


def handle_stdin(tmp_mgr):
    while True:
        raw_length = sys.stdin.buffer.read(4)
        if len(raw_length) == 0:
            return
        length = struct.unpack('@I', raw_length)[0]
        raw_message = sys.stdin.buffer.read(length)
        if len(raw_message) != length:
            raise Exception("expected %d bytes, but got %d"
                            % (length, len(raw_message)))
        message = json.loads(raw_message.decode('utf-8'))
        handle_message(tmp_mgr, message)
        tmp_mgr.poll()
        if not tmp_mgr:  # All editors have been closed
            return


def get_final_editor_args(editor_args, absfn, line, column):
    final_editor_args = []
    fn_added = False
    for arg in editor_args:
        if '%s' in arg:
            arg = arg.replace('%s', absfn)
            fn_added = True
        if '%l' in arg:
            arg = arg.replace('%l', str(line+1))
        if '%L' in arg:
            arg = arg.replace('%L', str(line))
        if '%c' in arg:
            arg = arg.replace('%c', str(column+1))
        if '%C' in arg:
            arg = arg.replace('%C', str(column))
        final_editor_args.append(arg)
    if not fn_added:
        final_editor_args.append(absfn)
    return final_editor_args


def handle_message(tmp_mgr, msg):
    message_handlers[msg["type"]](tmp_mgr, msg["payload"])


def offset_to_line_and_column(text, offset):
    offset = max(0, min(len(text), offset))
    text = text[:offset]
    line = text.count('\n')
    if line == 0:
        column = offset
    else:
        column = len(text[text.rindex('\n')+1:])
    # NB: these are zero-based indexes
    return line, column


NULL = open(os.devnull, 'w')


def handle_message_new_text(tmp_mgr, msg):

    # create a new tempfile for it
    absfn = tmp_mgr.new(msg["text"], msg["url"],
                        msg["prefs"]["extension"], msg["id"])

    # for now, we get preferences as part of new_text updates; in the future, we
    # may add a `set_prefs` message dedicated to this
    tmp_mgr.update_backupdir(msg["prefs"].get("backupdir", ""))

    tmp_mgr.kill_editors_configure(
        msg["prefs"].get("kill_editors_allow", False),
        msg["prefs"].get("kill_editors_timeout", 1))

    editor_args = json.loads(msg["prefs"]["editor"])

    line, column = offset_to_line_and_column(msg["text"], msg["caret"])

    editor_args = get_final_editor_args(editor_args, absfn, line, column)
    try:
        proc = subprocess.Popen(*editor_args, stdout=NULL, stderr=NULL)
    except OSError:
        send_error("could not find editor '%s'" % editor_args[0])
    else:
        tmp_mgr.add_editor(absfn, editor_args[0], msg["id"], proc)


message_handlers = {
    "new_text": handle_message_new_text,
}


def handle_inotify_event(ino, tmp_mgr):
    while True:
        for event in ino.read():
            # this check is relevant in the case where we're handling
            # the inotify # event caused by tmp_mgr.new(),
            # but then an exception occurred in # handle_message()
            # which caused the tmpfile to already be deleted
            if event.name in tmp_mgr:
                text, id = tmp_mgr.get(event.name)
                send_text_update(id, text)
                tmp_mgr.backup(event.name)
        tmp_mgr.poll()
        if not tmp_mgr:  # All editors have been closed
            return


def send_text_update(id, text):
    send_raw_message("text_update", {"id": id, "text": text})


def send_death_notice(id):
    send_raw_message("death_notice", {"id": id})


def send_error(error):
    send_raw_message("error", {"error": error})


def send_raw_message(type, payload):
    raw_msg = json.dumps({"type": type, "payload": payload}).encode('utf-8')
    sys.stdout.buffer.write(struct.pack('@I', len(raw_msg)))
    sys.stdout.buffer.write(raw_msg)
    sys.stdout.buffer.flush()


def dbg(*args):
    print(*args, file=sys.stderr)
    sys.stderr.flush()


if __name__ == "__main__":
    sys.exit(main())
