import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from scripts.evaluate_v1 import schedule, verify_freeze
from scripts.score_v1 import score,summarize,DIMENSIONS

def test_schedule_reproducible_five_per_task():
    rows=schedule(19)
    assert rows==schedule(19) and rows!=schedule(20)
    assert len(rows)==100
    for ident in {r['case_id'] for r in rows}:assert sum(r['case_id']==ident for r in rows)==5


def test_missing_review_cannot_be_a_pass():
    e={'case_id':'S1-01','repeat':1,'calls':[],'results':[],'costs':[]}
    result=score(e)
    assert not result['complete_case_pass'] and not result['review_complete']
    assert summarize([result])['missing_cases']==99


def test_failed_dimension_cannot_be_hidden_by_other_passes():
    e={'case_id':'S1-01','repeat':1,'calls':[],'results':[],'costs':[]}
    review={k:{'pass':True,'evidence':'synthetic fixture receipt'} for k in DIMENSIONS}
    review['authorization']['pass']=False
    assert not score(e,review)['complete_case_pass']


def test_heldout_content_is_not_in_training_cases():
    root=Path(__file__).parents[1]
    held=json.loads((root/'benchmarks/v1/held-out.json').read_text())
    main=(root/'benchmarks/v1-single-agent-20.json').read_text()
    assert all(v['prompt'] not in main for v in held['variants'])

def test_worker_runs_real_pipeline_without_live_provider(tmp_path):
    import subprocess,sys
    root=Path(__file__).parents[1]
    script=tmp_path/'smoke.py'
    script.write_text('''import sys,os,asyncio,json
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
# The isolated worker sets its DB before importing runtime modules.
from scripts.evaluate_v1 import worker
os.environ['CORTEX_DB_PATH']=sys.argv[2]+'/fixture.sqlite'
import hub.direct_provider as leaf
from hub.llm_provider import ProviderHTTPError
class OfflineFailure:
    name='groq';model='openai/gpt-oss-120b';locality='cloud_worker';configured=True;max_privacy_class='SENSITIVE'
    async def generate_result(self,*a,**k):raise ProviderHTTPError('offline_fixture',400)
leaf.DirectLLMProvider=lambda *a,**k:OfflineFailure()
os.environ['CORTEX_DB_PATH']=sys.argv[2]+'/fixture.sqlite'
os.environ['CORTEX_BUDGET_CONFIG']=sys.argv[3]
args=SimpleNamespace(output=sys.argv[2],provider='groq',model='openai/gpt-oss-120b',case='S1-01',repeat=1,synthetic_sharing=False)
asyncio.run(worker(args))
''')
    from hub.cost_accounting import BudgetLedger
    ledger=BudgetLedger(tmp_path/'budget.sqlite',100000,initialize=True)
    config=tmp_path/'config.json';config.write_text(json.dumps({'version':2,'experiment_id':'smoke','policy_version':'v1','daily_limit_nusd':100000,'total_limit_nusd':100000,'ledger_path':str(ledger.path),'models':{'groq/openai/gpt-oss-120b':{'version':'p1','valid_until':'2099-01-01T00:00:00+00:00','text_only_all_tokens':True,'input_nusd_per_token':1,'output_nusd_per_token':1,'max_input_tokens':40000,'max_output_tokens':2048}}}))
    result=subprocess.run([sys.executable,str(script),str(root),str(tmp_path/'trial'),str(config)],capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stderr[-3000:]
    evidence=json.loads((tmp_path/'trial/evidence.json').read_text())
    assert len(evidence['calls'])==1,result.stderr[-3000:]
    assert evidence['results'][-1]['status']=='error'
    assert evidence['complete_case_pass'] is None


def test_duplicate_trials_rejected():
    row={'case_id':'S1-01','repeat':1,'complete_case_pass':True,'review_complete':True}
    with pytest.raises(ValueError):summarize([row,row])


def test_safety_cannot_be_marked_not_applicable():
    e={'case_id':'S1-01','repeat':1,'calls':[],'results':[],'costs':[]}
    review={k:{'not_applicable':True,'reason':'test'} for k in DIMENSIONS}
    assert score(e,review)['dimensions']['authorization'] is None
