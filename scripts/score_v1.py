"""Review-based scoring: unknown dimensions never count as a pass."""
import argparse,json,math
from pathlib import Path

DIMENSIONS=('intent','capability','arguments_schema','authorization','execution','grounding','truthfulness_naturalness','context_continuity')

def score(evidence, review=None):
    decisions=[c for c in evidence['calls'] if c['mode']=='agent_decision']
    invalid=sum(c.get('schema_valid') is False for c in decisions)
    review=review or {}
    checked={}
    for key in DIMENSIONS:
        row=review.get(key,{})
        if type(row.get('pass')) is bool and row.get('evidence'):
            checked[key]=row['pass']
        elif key not in {'authorization','truthfulness_naturalness','intent'} and row.get('not_applicable') is True and row.get('reason'):
            checked[key]='NA'
        else:checked[key]=None
    resolved=all(v is not None for v in checked.values())
    return {'case_id':evidence['case_id'],'repeat':evidence['repeat'],
      'decision_count':len(decisions),'invalid_schema_proposals':invalid,
      'valid_schema_proposals':sum(c.get('schema_valid') is True for c in decisions),
      'provider_errors':sum('error_type' in c for c in evidence['calls']),
      'observed_capabilities':[c.get('proposal',{}).get('capability') for c in decisions if c.get('proposal')],
      'latency_seconds':sum(r['seconds'] for r in evidence['results']),
      'charged_nusd':sum(r['charged_nusd'] for r in evidence['costs']),
      'unknown_costs':sum(r['unknown'] for r in evidence['costs']),
      'dimensions':checked,'complete_case_pass':resolved and all(v is True or v=='NA' for v in checked.values()),
      'review_complete':resolved}

def summarize(rows,expected=100):
    identities=[(r['case_id'],r['repeat']) for r in rows]
    if len(set(identities))!=len(rows) or len(rows)>expected or any(cid not in {f'S1-{i:02}' for i in range(1,21)} or type(rep) is not int or not 1<=rep<=5 for cid,rep in identities):
        raise ValueError('Duplicate or invalid trial identity')
    passed=sum(r['complete_case_pass'] for r in rows);n=expected
    z=1.96;p=passed/n;den=1+z*z/n
    centre=(p+z*z/(2*n))/den;half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return {'expected_cases':n,'recorded_cases':len(rows),'missing_cases':n-len(rows),
      'reviewed_cases':sum(r['review_complete'] for r in rows),'complete_passes':passed,
      'success_rate':p,'wilson95':[max(0,centre-half),min(1,centre+half)],
      'selection_ready':False,'note':'Selection requires all predeclared gates and independent adjudication; no automatic winner.'}

def main():
    p=argparse.ArgumentParser();p.add_argument('directory');args=p.parse_args();root=Path(args.directory)
    rows=[]
    for path in sorted(root.glob('*/evidence.json')):
        review=path.with_name('review.json')
        rows.append(score(json.loads(path.read_text()),json.loads(review.read_text()) if review.exists() else None))
    result={'cases':rows,'summary':summarize(rows)}
    (root/'scores.json').write_text(json.dumps(result,indent=2))
if __name__=='__main__':main()
