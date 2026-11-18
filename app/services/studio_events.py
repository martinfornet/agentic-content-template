"""Outbound long-poll wakeup for the authorized local studio listener."""
import hashlib,json,time

def feedback_cursor(workflow):
    root=workflow.store.root
    paths=list((root/'reels/inbox').glob('*.json'))+list((root/'reels/projects').glob('*/project.json'))
    paths += [root/'music-plans.json',root/'visual-audits.json']
    values=[]
    for p in sorted(paths):
        try:stat=p.stat();values.append((str(p.relative_to(root)),stat.st_mtime_ns,stat.st_size))
        except FileNotFoundError:continue
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()

def wait_feedback(workflow,cursor,timeout=40):
    if cursor is not None and (not isinstance(cursor,str) or len(cursor)!=64):raise ValueError('Curseur de suivi invalide.')
    deadline=time.monotonic()+timeout
    while True:
        current=feedback_cursor(workflow)
        if current!=cursor or time.monotonic()>=deadline:return {'cursor':current,'changed':current!=cursor}
        time.sleep(.25)
