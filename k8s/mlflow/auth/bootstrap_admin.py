"""One-time bootstrap on a verified head, using DML-only credentials."""
import os,sys,logging,configparser
from pathlib import Path
from sqlalchemy import create_engine,text
from auth_config import load,uri,require,HEAD
logging.disable(logging.CRITICAL)
def main():
    p,s=load();url=uri(p,s);user=os.environ['MLFLOW_AUTH_ADMIN_USERNAME'];password=os.environ['MLFLOW_AUTH_ADMIN_PASSWORD'];require(bool(user and password))
    e=create_engine(url,hide_parameters=True)
    with e.connect() as c:
        require(c.execute(text('SELECT version_num FROM alembic_version_auth')).scalars().all()==[HEAD])
        rows=c.execute(text('SELECT password_hash,is_admin FROM users WHERE username=:u'),{'u':user}).all()
        if rows:
            from werkzeug.security import check_password_hash
            require(len(rows)==1 and rows[0][1] and check_password_hash(rows[0][0],password));print('Existing administrator verified');return 0
        require(c.execute(text('SELECT COUNT(*) FROM users')).scalar_one()==0)
    e.dispose();os.umask(0o077);path=Path('/run/mlflow-auth/auth.ini');path.parent.mkdir(parents=True,exist_ok=True)
    cfg=configparser.ConfigParser(interpolation=None);cfg['mlflow']={'default_permission':'NO_PERMISSIONS','database_uri':url.replace('%','%%')}
    with path.open('w') as f:cfg.write(f)
    os.environ['MLFLOW_AUTH_CONFIG_PATH']=str(path)
    from mlflow.server.auth import bootstrap_admin_user
    bootstrap_admin_user()
    e=create_engine(url,hide_parameters=True)
    from werkzeug.security import check_password_hash
    with e.connect() as c:
        row=c.execute(text('SELECT password_hash,is_admin FROM users WHERE username=:u'),{'u':user}).one();require(row[1] and check_password_hash(row[0],password))
    e.dispose();print('Administrator bootstrap verified');return 0
if __name__=='__main__':
    try:sys.exit(main())
    except Exception as e:print('Bootstrap rejected: '+type(e).__name__);sys.exit(1)
