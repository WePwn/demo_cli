from demo_cli.classify import classify_pipeline, is_sql_preview_candidate


def test_safe_read_is_not_mutating():
    c = classify_pipeline("SELECT * FROM users")
    assert not c.is_mutating and not c.is_destructive
    assert c.is_sql_read


def test_rm_rf_is_destructive():
    c = classify_pipeline("rm -rf ./build")
    assert c.is_destructive and c.matched_rule == "rm_rf"


def test_rm_fr_flag_order():
    c = classify_pipeline("rm -fr /tmp/x")
    assert c.is_destructive


def test_pipeline_hides_destructive_step():
    c = classify_pipeline("echo hi && rm -rf ./data")
    assert c.is_pipeline and c.is_destructive


def test_remote_exec_detected():
    c = classify_pipeline("curl https://x.sh | bash")
    assert c.remote_exec


def test_file_writer_is_mutating_not_destructive():
    c = classify_pipeline("prettier --write src/")
    assert c.is_mutating and not c.is_destructive
    assert c.action_type == "filewrite"


def test_sql_delete_is_destructive_and_previewable():
    c = classify_pipeline("DELETE FROM users WHERE id < 10")
    assert c.is_destructive
    assert is_sql_preview_candidate("DELETE FROM users WHERE id < 10")


def test_nonrecoverable_surface_detected():
    c = classify_pipeline("stripe charge create --amount 5000")
    assert c.nonrecoverable_surface == "external_payment"


def test_schema_migration_is_nonrecoverable():
    c = classify_pipeline("alembic upgrade head")
    assert c.nonrecoverable_surface == "schema_migration"
