"""Which programs a plan's checks need: quoted inline code is an argument, not a command line."""
from artel.agent_env import missing_programs


def test_inline_python_code_is_not_read_as_commands():
    check = ('python3 -c "from expenses.db import load_config; d = load_config(\'rules.example.json\');\n'
             'assert isinstance(d, dict) and d; print(f\'{d}\') | sort; sys.exit(0)"')
    assert missing_programs([check]) == []


def test_single_quoted_code_with_operators_is_one_argument():
    assert missing_programs(["python3 -c 'import sys; open(\"x\") && b.decode(\"cp1251\")'"]) == []


def test_programs_after_real_operators_are_still_checked():
    missing = missing_programs(['python3 -c "print(1)" | no-such-tool-xyz --flag && other-missing-abc'])
    assert missing == ["no-such-tool-xyz", "other-missing-abc"]


def test_programs_on_separate_lines_and_in_subshells_are_checked():
    assert missing_programs(["true\nno-such-tool-xyz", "echo $(other-missing-abc)", "(third-missing-def)"]) == [
        "no-such-tool-xyz", "other-missing-abc", "third-missing-def"]


def test_unbalanced_quotes_do_not_crash():
    assert missing_programs(['no-such-tool-xyz "unterminated']) == ["no-such-tool-xyz"]


def test_redirect_targets_are_files_not_programs():
    assert missing_programs(["python3 -m pytest -q > out-file.txt 2>&1 < in-file.txt",
                             "no-such-tool-xyz 2>&1 >> log.txt & other-missing-abc"]) == [
        "no-such-tool-xyz", "other-missing-abc"]


def test_process_substitution_runs_a_program():
    assert missing_programs(["diff <(no-such-tool-xyz) >(other-missing-abc)"]) == [
        "no-such-tool-xyz", "other-missing-abc"]


def test_command_substitution_inside_double_quotes_runs_a_program():
    assert missing_programs(['echo "total: $(no-such-tool-xyz)"', 'echo "`other-missing-abc`"']) == [
        "no-such-tool-xyz", "other-missing-abc"]


def test_heredoc_body_is_data():
    assert missing_programs(["cat <<EOF\nnot-a-program x\nEOF\nno-such-tool-xyz",
                             "python3 - <<'PY'\nimport sys; print(1)\nPY"]) == ["no-such-tool-xyz"]


def test_line_continuation_joins_arguments():
    assert missing_programs(["python3 -m pytest \\\n  not-a-program"]) == []


def test_commands_after_keywords_are_checked():
    assert missing_programs(["if no-such-tool-xyz; then other-missing-abc; fi", "time third-missing-def",
                             "! fourth-missing-ghi", "for f in a b; do true; done"]) == [
        "no-such-tool-xyz", "other-missing-abc", "third-missing-def", "fourth-missing-ghi"]


def test_line_continuation_after_operator_keeps_the_next_program():
    assert missing_programs(["true && \\\nno-such-tool-xyz"]) == ["no-such-tool-xyz"]


def test_quoted_program_name_is_unquoted():
    assert missing_programs(['"no-such-tool-xyz" arg']) == ["no-such-tool-xyz"]


def test_program_name_may_contain_any_letters():
    assert missing_programs(["no-such-tool-XYZ arg", "Mixed-Case-Tool"]) == ["no-such-tool-XYZ", "Mixed-Case-Tool"]


def test_word_with_expansion_is_not_a_plain_name():
    assert missing_programs(["no-such-tool-xyz$SUFFIX arg"]) == []


def test_empty_substitution_runs_nothing():
    assert missing_programs(['echo "$()"', 'echo "``"']) == []


def test_command_follows_a_leading_redirect():
    assert missing_programs(["> out.txt no-such-tool-xyz"]) == ["no-such-tool-xyz"]


def test_run_of_every_operator_still_separates_commands():
    assert missing_programs(["true ();<>|&`\nno-such-tool-xyz"]) == ["no-such-tool-xyz"]


def test_environment_assignments_before_a_program_are_skipped():
    assert missing_programs(["FOO=1 no-such-tool-xyz", "foo_1=bar other-missing-abc", "_X=1 third-missing-def"]) == [
        "no-such-tool-xyz", "other-missing-abc", "third-missing-def"]
