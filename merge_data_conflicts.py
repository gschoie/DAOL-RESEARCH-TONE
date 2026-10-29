"""커밋백 리베이스 충돌 해결 — 두 런이 겹쳐 데이터 커밋이 부딪힐 때 워크플로가 호출한다.

증분 캐시(dict 기반 JSON)는 origin 쪽과 이 런 쪽을 합집합(키 충돌은 이 런 우선)으로 합치고,
매 런 통째로 재생성되는 산출물(history·v2·summary·radar·brief·status 등)은 이 런 결과를 쓴다.
리베이스 중 stage 2 = origin(upstream), stage 3 = 재생 중인 이 런의 커밋.
"""
import json, subprocess, sys

MERGE_CACHES = {
    'data/daol_ai_analysis.json', 'data/daol_strategy_ai.json', 'data/daol_pdf_text_cache.json',
    'data/naver_consensus_cache.json', 'data/price_close_cache.json', 'data/naver_fundamentals.json',
    'data/valuation_history.json', 'data/ked_street.json', 'data/daol_macro_brief.json',
}


def staged(stage, path):
    return json.loads(subprocess.check_output(['git', 'show', f':{stage}:{path}'], text=True))


def main():
    files = subprocess.check_output(['git', 'diff', '--name-only', '--diff-filter=U'], text=True).split()
    if not files:
        print('merge_data_conflicts: 충돌 파일 없음'); return 0
    for f in files:
        try:
            if f in MERGE_CACHES:
                base, ours = staged(2, f), staged(3, f)
                if isinstance(base, dict) and isinstance(ours, dict):
                    merged = {**base, **ours}
                    note = f'합집합 {len(base)}+{len(ours)}→{len(merged)}'
                else:
                    merged, note = ours, '이 런 우선(비-dict)'
                with open(f, 'w', encoding='utf-8') as fh:
                    json.dump(merged, fh, ensure_ascii=False, separators=(',', ':'))
            else:
                subprocess.run(['git', 'checkout', '--theirs', '--', f], check=True)
                note = '이 런 결과'
        except Exception as exc:  # 어느 한 파일이라도 못 풀면 호출 측이 rebase --abort 한다
            print(f'merge_data_conflicts: {f} 해결 실패: {exc}'); return 1
        subprocess.run(['git', 'add', f], check=True)
        print(f'merge_data_conflicts: {f} — {note}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
