-- Restores the 0001 guards, then removes the budget columns.

CREATE OR REPLACE FUNCTION investigations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status IN ('completed', 'failed', 'reconciliation_required') THEN
        RAISE EXCEPTION 'investigation % is terminal and cannot change', OLD.investigation_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status = 'accepted' AND OLD.status <> 'accepted' THEN
        RAISE EXCEPTION 'investigation % cannot return to accepted', OLD.investigation_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.fencing_token < OLD.fencing_token THEN
        RAISE EXCEPTION 'fencing token of % cannot decrease', OLD.investigation_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.incident IS DISTINCT FROM OLD.incident AND NEW.incident IS NOT NULL THEN
        RAISE EXCEPTION 'the incident of % can only be cleared', OLD.investigation_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.investigation_id, NEW.principal_id, NEW.idempotency_key, NEW.request_hash,
        NEW.workflow_version, NEW.prompt_version, NEW.max_llm_calls, NEW.max_tool_calls,
        NEW.max_output_tokens, NEW.deadline_at, NEW.created_at)
        IS DISTINCT FROM
       (OLD.investigation_id, OLD.principal_id, OLD.idempotency_key, OLD.request_hash,
        OLD.workflow_version, OLD.prompt_version, OLD.max_llm_calls, OLD.max_tool_calls,
        OLD.max_output_tokens, OLD.deadline_at, OLD.created_at) THEN
        RAISE EXCEPTION 'immutable fields of % cannot change', OLD.investigation_id
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION operations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.operation_id, NEW.investigation_id, NEW.operation_type, NEW.attempt,
        NEW.fencing_token, NEW.model_alias, NEW.gateway_request_id, NEW.max_tokens,
        NEW.started_at)
        IS DISTINCT FROM
       (OLD.operation_id, OLD.investigation_id, OLD.operation_type, OLD.attempt,
        OLD.fencing_token, OLD.model_alias, OLD.gateway_request_id, OLD.max_tokens,
        OLD.started_at) THEN
        RAISE EXCEPTION 'immutable fields of operation % cannot change', OLD.operation_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.state = OLD.state THEN
        -- The only change allowed without a transition: clearing a recorded reply.
        IF OLD.state = 'succeeded' AND NEW.reply IS NULL AND
           (NEW.may_have_executed, NEW.failure_code, NEW.gateway_status,
            NEW.gateway_error_code, NEW.usage, NEW.dispatched_at, NEW.completed_at)
           IS NOT DISTINCT FROM
           (OLD.may_have_executed, OLD.failure_code, OLD.gateway_status,
            OLD.gateway_error_code, OLD.usage, OLD.dispatched_at, OLD.completed_at) THEN
            RETURN NEW;
        END IF;
    ELSIF (OLD.state = 'intent' AND NEW.state IN ('dispatched', 'abandoned'))
       OR (OLD.state = 'dispatched' AND NEW.state IN ('succeeded', 'failed', 'uncertain')) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'operation % cannot go from % to %', OLD.operation_id, OLD.state, NEW.state
        USING ERRCODE = 'check_violation';
END;
$$;

ALTER TABLE operations
    DROP CONSTRAINT operations_settled_only_when_closed,
    DROP COLUMN settled_micro_usd,
    DROP COLUMN reserved_micro_usd;

ALTER TABLE investigations
    DROP CONSTRAINT investigations_settled_when_finished,
    DROP COLUMN settled_micro_usd,
    DROP COLUMN reserved_micro_usd,
    DROP COLUMN pricing,
    DROP COLUMN budget_micro_usd;
