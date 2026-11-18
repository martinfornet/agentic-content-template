"""Recoverable multi-file delivery. Readers share a lock; incomplete commits roll back."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4
from app.services.acquisition_store import AcquisitionStore


def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

@contextmanager
def delivery_lock(workflow, exclusive=False):
    workflow.store.root.mkdir(parents=True,exist_ok=True)
    with (workflow.store.root/'delivery.lock').open('a') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def atomic_copy(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    temp=target.with_name(target.name+'.'+uuid4().hex+'.tmp')
    try:
        with Path(source).open('rb') as src,temp.open('wb') as dst:
            shutil.copyfileobj(src,dst);dst.flush();os.fsync(dst.fileno())
        temp.replace(target)
    finally:temp.unlink(missing_ok=True)


def rollback(workflow, folder, journal):
    for row in reversed(journal['files']):
        target=workflow.store.path(row['path'])
        if row['existed']:atomic_copy(folder/row['backup'],target)
        else:target.unlink(missing_ok=True)
    journal['status']='rolled_back'
    AcquisitionStore(folder).write('journal.json',journal)


def recover_deliveries(workflow):
    recovered=[]
    with delivery_lock(workflow,True):
        for path in (workflow.store.root/'deliveries').glob('*/*/journal.json'):
            journal=json.loads(path.read_text())
            if journal['status']=='applying':rollback(workflow,path.parent,journal);recovered.append(path.parent.name)
    return recovered


def commit_files(workflow, ident, files):
    """Caller holds exclusive delivery lock. Preimages exist before the first mutation."""
    store=AcquisitionStore(workflow.store.root/'deliveries'/ident/uuid4().hex)
    rows=[]
    for index,(relative,source) in enumerate(files.items()):
        target=workflow.store.path(relative)
        backup='previous/'+str(index)
        if target.exists():atomic_copy(target,store.path(backup))
        rows.append({'path':relative,'sha256':sha256(source),'existed':target.exists(),'backup':backup})
    journal={'status':'applying','files':rows}
    store.write('journal.json',journal)
    try:
        for row in rows:
            atomic_copy(files[row['path']],workflow.store.path(row['path']))
            if sha256(workflow.store.path(row['path']))!=row['sha256']:raise ValueError('Empreinte différente après copie.')
    except BaseException:
        rollback(workflow,store.root,journal)
        raise
    journal['status']='committed';store.write('journal.json',journal)
    return rows
