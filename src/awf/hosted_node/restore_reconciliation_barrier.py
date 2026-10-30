"""Invoker guards consume exact transaction-local restore journal transitions."""

from sqlalchemy import text
from sqlalchemy.engine import Connection


def replace_restore_barrier(connection: Connection) -> None:
    connection.execute(
        text("""
    CREATE OR REPLACE FUNCTION awf_restore_transition(p_family text, before_row jsonb, after_row jsonb)
    RETURNS boolean LANGUAGE plpgsql AS $$
    DECLARE journal record; entry record; old_hash text; new_hash text; identifier text;
    BEGIN
        IF to_regclass('hosted_restore_reconciliations') IS NULL THEN RETURN false; END IF;
        SELECT * INTO journal FROM hosted_restore_reconciliations
        WHERE state='applying' AND transaction_id=txid_current()
          AND identity = NULLIF(current_setting('awf.node_identity',true),'')::jsonb
        FOR UPDATE;
        IF NOT FOUND THEN RETURN false; END IF;
        old_hash := encode(sha256(convert_to(before_row::text,'UTF8')),'hex');
        new_hash := encode(sha256(convert_to(after_row::text,'UTF8')),'hex');
        identifier := coalesce(after_row->>'id',after_row->>'row_id',after_row->>'singleton',
                               before_row->>'id',before_row->>'row_id',before_row->>'singleton');
        SELECT e.ordinality INTO entry FROM jsonb_array_elements(journal.mutations) WITH ORDINALITY e(value,ordinality)
        WHERE e.value->>'family'=p_family AND e.value->>'row_id'=identifier
          AND e.value->>'before_sha256'=old_hash AND e.value->>'after_sha256'=new_hash
          AND e.value->>'used'='false';
        IF NOT FOUND THEN RETURN false; END IF;
        UPDATE hosted_restore_reconciliations
        SET mutations=jsonb_set(mutations,ARRAY[(entry.ordinality-1)::text,'used'],'true'::jsonb)
        WHERE id=journal.id;
        RETURN true;
    END $$
    """)
    )
    connection.execute(
        text("""
    CREATE OR REPLACE FUNCTION awf_restore_row_held(p_family text, value jsonb)
    RETURNS boolean LANGUAGE plpgsql AS $$
    DECLARE owner_id text; v_workspace text; v_task text; v_subscription text;
    BEGIN
        IF to_regclass('hosted_restore_row_holds') IS NULL OR value='{}'::jsonb THEN RETURN false; END IF;
        owner_id := NULLIF(current_setting('awf.node_identity',true),'')::jsonb->>'owner';
        IF EXISTS(SELECT 1 FROM hosted_restore_row_holds h WHERE h.owner=owner_id
                  AND h.family=p_family AND h.row_id=value->>'id' AND h.state='held') THEN RETURN true; END IF;
        v_workspace := CASE WHEN p_family='workspaces' THEN value->>'id' ELSE value->>'workspace_id' END;
        v_task := CASE WHEN p_family='tasks' THEN value->>'id' ELSE value->>'task_id' END;
        v_subscription := CASE WHEN p_family='callback_subscriptions' THEN value->>'id' ELSE value->>'subscription_id' END;
        IF EXISTS(SELECT 1 FROM hosted_restore_row_holds h WHERE h.owner=owner_id AND h.state='held'
                  AND ((h.family='tasks' AND (h.row_id=v_task OR EXISTS(
                        SELECT 1 FROM task_attempts a WHERE a.task_id=h.row_id
                          AND (a.id=value->>'attempt_id' OR a.workspace_id=v_workspace))))
                    OR (h.family='callback_subscriptions' AND h.row_id=v_subscription))) THEN RETURN true; END IF;
        IF p_family='callback_deliveries' AND EXISTS(
            SELECT 1 FROM workspaces w WHERE w.task_policy::jsonb #>> '{hosted_recovery,disposition}'='attention'
             AND w.task_policy::jsonb #>> '{hosted_recovery,restore_id}' IS NOT NULL
             AND (w.id=v_workspace OR EXISTS(SELECT 1 FROM operations o WHERE o.id=value->>'operation_id' AND o.workspace_id=w.id)
               OR EXISTS(SELECT 1 FROM merge_candidates m WHERE m.id=value->>'merge_candidate_id' AND m.workspace_id=w.id)
               OR EXISTS(SELECT 1 FROM workspace_events e WHERE e.id=value->>'source_id' AND e.workspace_id=w.id))
        ) THEN RETURN true; END IF;
        RETURN false;
    END $$
    """)
    )
    connection.execute(
        text("""
    CREATE OR REPLACE FUNCTION awf_restore_metadata_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE before_row jsonb; after_row jsonb; gate hosted_node_lifecycle%ROWTYPE;
    BEGIN
        before_row := CASE WHEN TG_OP='INSERT' THEN '{}'::jsonb ELSE to_jsonb(OLD) END;
        after_row := CASE WHEN TG_OP='DELETE' THEN '{}'::jsonb ELSE to_jsonb(NEW) END;
        IF TG_TABLE_NAME='hosted_restore_reconciliations' THEN
            IF before_row->>'state'='committed' AND before_row IS DISTINCT FROM after_row THEN
                RAISE EXCEPTION 'RESTORE_JOURNAL_IMMUTABLE' USING ERRCODE='55000';
            END IF;
        ELSIF TG_TABLE_NAME='hosted_node_lifecycle' THEN
            IF (before_row->'restore_quarantine' IS NOT NULL AND before_row->'restore_quarantine'<>'null'::jsonb
                AND (TG_OP='DELETE' OR after_row->'restore_quarantine'='null'::jsonb))
               OR (before_row #>> '{restore_quarantine,state}'='held'
                   AND before_row->'restore_quarantine' IS DISTINCT FROM after_row->'restore_quarantine')
               OR coalesce(before_row->>'restore_reconciliation_id','') IS DISTINCT FROM coalesce(after_row->>'restore_reconciliation_id','') THEN
                IF NOT awf_restore_transition(TG_TABLE_NAME,before_row,after_row) THEN
                    RAISE EXCEPTION 'RESTORE_RECONCILIATION_REQUIRED' USING ERRCODE='55000';
                END IF;
            END IF;
        ELSE
            SELECT * INTO gate FROM hosted_node_lifecycle WHERE singleton=1 FOR UPDATE;
            IF FOUND THEN
                IF gate.identity IS DISTINCT FROM NULLIF(current_setting('awf.node_identity',true),'')::jsonb
                   OR NOT awf_restore_transition(TG_TABLE_NAME,before_row,after_row) THEN
                    RAISE EXCEPTION 'RESTORE_RECONCILIATION_REQUIRED' USING ERRCODE='55000';
                END IF;
            END IF;
        END IF;
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$
    """)
    )
    if connection.scalar(text("SELECT to_regclass('hosted_restore_row_holds')")) is not None:
        for table in (
            "hosted_restore_row_holds",
            "hosted_node_lifecycle",
            "hosted_restore_reconciliations",
        ):
            connection.execute(text(f"DROP TRIGGER IF EXISTS awf_restore_metadata ON {table}"))
            connection.execute(
                text(
                    f"CREATE TRIGGER awf_restore_metadata BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION awf_restore_metadata_guard()"
                )
            )
