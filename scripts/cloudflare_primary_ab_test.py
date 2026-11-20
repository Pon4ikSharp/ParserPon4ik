import json, os, re, time
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
PROMPT=(ROOT/"docs"/"PRIMARY_INFO_V1_PROMPT.md").read_text(encoding="utf-8")
BATCH_ID=os.getenv("CF_TEST_BATCH_ID","20260915T131347Z_9c383594")
MODEL=os.getenv("CF_WORKERS_AI_MODEL","@cf/openai/gpt-oss-20b")
def load_local_env():
    path=ROOT/".env.cloudflare"
    if not path.exists():
        return {}
    data={}
    for line in path.read_text(encoding="utf-8").splitlines():
        line=line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k,v=line.split("=",1)
        data[k.strip()]=v.strip().strip('"').strip("'")
    return data

local_env=load_local_env()
ACCOUNT_ID=(os.getenv("CLOUDFLARE_ACCOUNT_ID") or local_env.get("CLOUDFLARE_ACCOUNT_ID","")).strip()
TOKEN=(os.getenv("CLOUDFLARE_API_TOKEN") or local_env.get("CLOUDFLARE_API_TOKEN","")).strip()

if not ACCOUNT_ID or not TOKEN:
    raise SystemExit("Create .env.cloudflare with CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN first.")

batch_path=ROOT/"runtime"/"primary_batches"/"inbox"/f"{BATCH_ID}.json"
gold_path=ROOT/"runtime"/"primary_batches"/"results"/f"{BATCH_ID}.result.json"
if not batch_path.exists() or not gold_path.exists():
    raise SystemExit(f"Missing A/B source or gold batch: {BATCH_ID}")

batch=json.loads(batch_path.read_text(encoding="utf-8"))
gold=json.loads(gold_path.read_text(encoding="utf-8"))
items=batch["items"]

user_payload={
    "batch_id": BATCH_ID,
    "instruction": "Classify every item using the contract. Return one JSON array only, exactly one result per source_document_id.",
    "items": items,
}
url=f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1/chat/completions"
body={
    "model": MODEL,
    "messages":[
        {"role":"system","content":PROMPT},
        {"role":"user","content":json.dumps(user_payload,ensure_ascii=False)}
    ],
    "temperature":0,
    "max_tokens":16000
}

t0=time.time()
with httpx.Client(timeout=240) as client:
    resp=client.post(url,headers={"Authorization":f"Bearer {TOKEN}","Content-Type":"application/json"},json=body)
elapsed=time.time()-t0
print("HTTP",resp.status_code,"seconds",round(elapsed,2))
if resp.status_code>=400:
    print(resp.text[:4000])
    raise SystemExit(2)

raw=resp.json()
content=raw["choices"][0]["message"]["content"].strip()
fence=chr(96)*3
if content.startswith(fence):
    content=content[len(fence):].strip()
    if content.lower().startswith("json"):
        content=content[4:].strip()
    if content.endswith(fence):
        content=content[:-len(fence)].strip()
try:
    cf_results=json.loads(content)
except Exception:
    outdir=ROOT/"runtime"/"cloudflare_ab_test"
    outdir.mkdir(parents=True,exist_ok=True)
    bad=outdir/f"{BATCH_ID}.{MODEL.split('/')[-1]}.raw.txt"
    bad.write_text(content,encoding="utf-8")
    print("Could not parse model JSON; raw saved:",bad)
    raise

if isinstance(cf_results,dict) and "results" in cf_results:
    cf_results=cf_results["results"]
if not isinstance(cf_results,list):
    raise SystemExit("Cloudflare response is not a JSON array")

gold_by={int(x["source_document_id"]):x for x in gold["results"]}
cf_by={int(x["source_document_id"]):x for x in cf_results if "source_document_id" in x}
all_ids=[int(x["source_document_id"]) for x in items]
missing=[x for x in all_ids if x not in cf_by]
agreement=0
false_negative=[]
false_positive=[]
for did in all_ids:
    if did not in cf_by or did not in gold_by:
        continue
    gd=gold_by[did].get("decision")
    cd=cf_by[did].get("decision")
    agreement += int(gd==cd)
    if gd!="NOT_RELEVANT" and cd=="NOT_RELEVANT":
        false_negative.append(did)
    if gd=="NOT_RELEVANT" and cd!="NOT_RELEVANT":
        false_positive.append(did)

out={
    "batch_id":BATCH_ID,
    "model":MODEL,
    "elapsed_seconds":round(elapsed,2),
    "item_count":len(all_ids),
    "returned":len(cf_by),
    "missing":missing,
    "decision_agreement":agreement,
    "decision_agreement_pct":round(100*agreement/max(1,len(all_ids)-len(missing)),1),
    "false_negative_vs_gold":false_negative,
    "false_positive_vs_gold":false_positive,
    "usage":raw.get("usage",{}),
    "cloudflare_results":cf_results
}
outdir=ROOT/"runtime"/"cloudflare_ab_test"
outdir.mkdir(parents=True,exist_ok=True)
outfile=outdir/f"{BATCH_ID}.{MODEL.split('/')[-1]}.json"
outfile.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps({k:v for k,v in out.items() if k!="cloudflare_results"},ensure_ascii=False,indent=2))
print("Saved",outfile)
