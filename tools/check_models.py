"""One tiny structured-output request per selected Codex profile. No bot calls."""
import argparse,json,shutil,subprocess,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from app import codex_runtime
from app.db import ROOT

def main():
    load_dotenv(ROOT/'.env')
    p=argparse.ArgumentParser();p.add_argument('roles',nargs='*',default=['collector','analyst','editor'],choices=['collector','analyst','editor']);a=p.parse_args()
    executable=shutil.which('codex') or shutil.which('codex.cmd')
    if not executable:raise RuntimeError('Codex CLI не знайдений. Перевірте команду codex --version.')
    schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
    errors=0
    with tempfile.TemporaryDirectory(prefix='ponchik-astra-') as temp:
        path=Path(temp)/'schema.json';path.write_text(json.dumps(schema),encoding='utf-8')
        for role in a.roles:
            env=codex_runtime.environment(role)
            result=subprocess.run([executable,'exec','--ephemeral','--skip-git-repo-check','--sandbox','read-only',
                    '--model',codex_runtime.model(role),'--output-schema',str(path),'-'],
                    input='Return exactly {"ok":true}. Do not read files, search or call tools.',
                    text=True,encoding='utf-8',capture_output=True,timeout=180,env=env,cwd=temp)
            try:passed=result.returncode==0 and json.loads(result.stdout).get('ok') is True
            except (ValueError,AttributeError):passed=False
            print(role,codex_runtime.model(role),'OK' if passed else 'FAILED')
            if not passed:
                errors+=1
                # Credentials and CLI logs are deliberately not printed.
                print('Перевірте авторизацію цього профілю через tools/codex_login.ps1. Код завершення:',result.returncode)
    raise SystemExit(1 if errors else 0)

if __name__=='__main__':main()
