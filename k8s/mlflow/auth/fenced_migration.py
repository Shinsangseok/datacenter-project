"""One physical session for lock and migrations; Secret-file credentials, no reconnect."""
from auth_config import load, connect_args, require, HEAD
import os,sys,json,time,hashlib,re,logging,tempfile,shutil
from pathlib import Path
import pymysql
from sqlalchemy import create_engine,event,text
from sqlalchemy.pool import NullPool
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.command import upgrade
from mlflow.server.auth.db import utils
logging.disable(logging.CRITICAL)
def emit(kind,**kw):
 print(json.dumps(dict(time=time.time(),executor=P['executor'],event=kind,**kw)),flush=True)
def snapshot(c):
 names=[x[0] for x in c.exec_driver_sql('SHOW TABLES')];out={}
 for n in sorted(names):
  assert re.fullmatch('[a-zA-Z0-9_]+',n)
  ddl=c.exec_driver_sql('SHOW CREATE TABLE `'+n+'`').one()[1]
  rows=[list(x) for x in c.exec_driver_sql('SELECT * FROM `'+n+'`')]
  out[n]={'schema':hashlib.sha256(ddl.encode()).hexdigest(),'data':hashlib.sha256(json.dumps(sorted(rows,key=lambda x:json.dumps(x,default=str)),default=str).encode()).hexdigest(),'rows':len(rows)}
 rev=list(c.exec_driver_sql('SELECT version_num FROM alembic_version_auth').scalars()) if 'alembic_version_auth' in names else []
 return {'tables':out,'revision':rev}
def main():
 global P
 policy,secret=load();P={'executor':os.environ.get('EXECUTOR_ID','auth-migration'),'db':secret['database']}
 expected=json.loads((Path(__file__).parent/'migration-hashes.json').read_text())
 root=Path(utils._get_alembic_dir())
 require(all(hashlib.sha256((root/n).read_bytes()).hexdigest()==h for n,h in expected.items()))
 connects=0;cid=None;locked=False;ddl=0
 def creator():
  nonlocal connects
  connects+=1
  if connects!=1:
   emit('reconnect_denied',attempt=connects);raise RuntimeError('automatic reconnect forbidden')
  return pymysql.connect(**connect_args(policy,secret))
 e=create_engine('mysql+pymysql://',creator=creator,poolclass=NullPool,hide_parameters=True)
 key='mlflow-auth-migration:'+P['db']
 @event.listens_for(e,'before_cursor_execute')
 def audit(conn,cursor,stmt,params,ctx,many):
  nonlocal ddl
  op=stmt.strip().split()[0].upper() if stmt.strip() else ''
  if op in ('CREATE','ALTER','DROP','INSERT','UPDATE','DELETE'):
   require(locked and conn.connection.driver_connection.thread_id()==cid)
   cursor.execute('SELECT CONNECTION_ID(),IS_USED_LOCK(%s)',(key,))
   actual,owner=cursor.fetchone();require(actual==owner==cid)
   if op in ('CREATE','ALTER','DROP'):ddl+=1
   emit('sql_enter',cid=cid,owner=owner,op=op,ddl=ddl)
 @event.listens_for(e,'after_cursor_execute')
 def after(conn,cursor,stmt,params,ctx,many):
  op=stmt.strip().split()[0].upper() if stmt.strip() else ''
  if op in ('CREATE','ALTER','DROP','INSERT','UPDATE','DELETE'):emit('sql_done',cid=cid,op=op)
 try:
  with e.connect() as c:
   cid=c.exec_driver_sql('SELECT CONNECTION_ID()').scalar_one();emit('connected',cid=cid)
   if c.execute(text('SELECT GET_LOCK(:k,0)'),{'k':key}).scalar_one()!=1:
    emit('duplicate_denied',cid=cid,ddl=ddl);return 20
   locked=True;emit('locked',cid=cid,owner=cid)
   c.exec_driver_sql('SET SESSION lock_wait_timeout=15')
   before=snapshot(c);emit('preflight',snapshot=before)
   known=json.loads(Path(os.environ.get('AUTH_CHECKPOINTS','/etc/mlflow-auth/policy/checkpoints.json')).read_text())
   if not before['tables']:require(policy.get('allow_empty') is True)
   if before['tables'] and before not in known:
    emit('unknown_partial_abort',ddl=ddl);return 30
   c.commit()
   tmp=Path(tempfile.mkdtemp(prefix='fence-alembic-'))
   try:
    shutil.copytree(utils._get_alembic_dir(),tmp,dirs_exist_ok=True)
    (tmp/'env.py').write_text('from alembic import context\nfrom mlflow.server.auth.db.models import Base\nc=context.config.attributes["connection"]\ncontext.configure(connection=c,target_metadata=Base.metadata,version_table="alembic_version_auth")\nwith context.begin_transaction():\n context.run_migrations()\n')
    cfg=Config();cfg.set_main_option('script_location',str(tmp));cfg.attributes['connection']=c
    script=ScriptDirectory.from_config(cfg)
    require(script.get_heads()==[HEAD])
    revisions=list(reversed(list(script.walk_revisions())))
    current=before['revision'][0] if before['revision'] else None
    pending=current is None
    for rev in revisions:
     if not pending:
      if rev.revision==current:pending=True
      continue
     require(c.execute(text('SELECT IS_USED_LOCK(:k)'),{'k':key}).scalar_one()==cid)
     emit('step_enter',revision=rev.revision,cid=cid)
     upgrade(cfg,rev.revision);c.commit()
     snap=snapshot(c);c.commit();emit('step_complete',revision=rev.revision,snapshot=snap,cid=cid)
    emit('complete',snapshot=snapshot(c),cid=cid,ddl=ddl,connect_attempts=connects)
   finally:shutil.rmtree(tmp)
   c.commit();require(c.execute(text('SELECT RELEASE_LOCK(:k)'),{'k':key}).scalar_one()==1)
   locked=False;emit('released',cid=cid)
  return 0
 except Exception as ex:
  orig=getattr(ex,'orig',ex);code=orig.args[0] if orig.args and isinstance(orig.args[0],int) else None
  emit('aborted',cid=cid,error=type(ex).__name__,mysql_code=code,ddl=ddl,connect_attempts=connects)
  return 1
 finally:e.dispose()
if __name__=='__main__':
 try:sys.exit(main())
 except Exception as ex:
  print(json.dumps({'event':'preflight_failed','error':type(ex).__name__}));sys.exit(1)
