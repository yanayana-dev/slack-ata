#!/usr/bin/env python3
"""run_tests.py — slack-listener 全テストのランナー。

背景（2026-11-26、Neiroさんの中-6修正を採用）:
  Neiroさんの公開リポジトリで、旧ランナー run_tests.py が slack-poll.py
  の 11件だけを内包しながら README のゲートを通過していた穴が見つかり、
  新ランナーは unittest discover と手作り検査スクリプトの両方を走らせ、
  1件でも失敗すれば exit 1 で終わる形に修正された。
  こちらはもっと素朴な状態だった: ランナー自体が存在せず、テストは
  ファイル単位で手動実行していた。tests/ には unittest を使うファイルは
  1つもなく、全 37ファイルが手作りスクリプト（`if __name__ ==
  "__main__": sys.exit(main())`、失敗時は exit 1）なので、unittest
  discover では main() が走らず exit 0 になってしまう。つまり discover
  を回しても「全通過」に見えてしまう、Neiroさんの旧ランナーと同型の穴
  がある。そこでこのランナーは tests/test_*.py を1本ずつサブプロセスで
  実行し、1本でも失敗すれば exit 1 で終わる。

使い方:
  python3 run_tests.py            # 全テストを実行、要約を表示
  python3 run_tests.py -v         # 各ファイルの出力もそのまま表示
  python3 run_tests.py --list     # 対象ファイルの一覧だけ表示
終了コード: 全ファイル通過で 0、1本でも失敗・異常終了すれば 1。
"""
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS_DIR = HERE / "tests"

SUMMARY_RE = re.compile(r"(\d+) passed, (\d+) failed")


def discover():
    return sorted(TESTS_DIR.glob("test_*.py"))


def run_one(path, verbose):
    proc = subprocess.run(
        [sys.executable, str(path)],
        capture_output=True, text=True, timeout=300,
    )
    out = proc.stdout + proc.stderr
    if verbose:
        print(f"--- {path.name} (exit {proc.returncode}) ---")
        print(out, end="")
    passed = failed = None
    for line in out.splitlines():
        m = SUMMARY_RE.search(line)
        if m:
            passed, failed = int(m.group(1)), int(m.group(2))
    return proc.returncode, passed, failed


def main(argv):
    verbose = "-v" in argv or "--verbose" in argv
    files = discover()
    if "--list" in argv:
        for f in files:
            print(f.name)
        return 0
    if not files:
        print("FAIL: tests/test_*.py が見つかりません")
        return 1
    total_passed = total_failed = 0
    failed_files = []
    for f in files:
        try:
            rc, passed, failed = run_one(f, verbose)
        except subprocess.TimeoutExpired:
            print(f"FAIL {f.name}: タイムアウト (300秒)")
            failed_files.append(f.name)
            continue
        if passed is not None:
            total_passed += passed
            total_failed += failed or 0
        if rc != 0 or passed is None or (failed or 0) > 0:
            detail = f"exit {rc}" if passed is None else (
                f"{passed} passed, {failed} failed")
            print(f"FAIL {f.name}: {detail}")
            failed_files.append(f.name)
        else:
            print(f"ok   {f.name}: {passed} passed")
    print(f"\n{len(files)} files, {total_passed} checks passed, "
          f"{total_failed} failed")
    if failed_files:
        print(f"FAILED: {', '.join(failed_files)}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
