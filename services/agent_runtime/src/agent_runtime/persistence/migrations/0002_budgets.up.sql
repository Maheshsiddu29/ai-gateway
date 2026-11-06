-- Per-investigation spending admission in integer micro-USD.
-- See docs/agent-durable-execution.md#budgets. Investigations accepted before this
-- migration get a budget of 0 and no prices, so no further model call is admitted
-- for them: unknown cost is never treated as zero.

ALTER TABLE investigations
    ADD COLUMN budget_micro_usd bigint NOT NULL DEFAULT 0 CHECK (budget_micro_usd >= 0),
    -- The prices, per model alias, in force when the investigation was accepted.
    ADD COLUMN pricing jsonb CHECK (pricing IS NULL OR jsonb_typeof(pricing) = 'object'),
    -- Estimated cost of operations admitted but not settled: open calls, and calls
    -- whose outcome is unknown.
    ADD COLUMN reserved_micro_usd bigint NOT NULL DEFAULT 0 CHECK (reserved_micro_usd >= 0),
    ADD COLUMN settled_micro_usd bigint NOT NULL DEFAULT 0 CHECK (settled_micro_usd >= 0),
    -- Only reconciliation_required may end with an unresolved liability.
    ADD CONSTRAINT investigations_settled_when_finished
        CHECK (status NOT IN ('completed', 'failed') OR reserved_micro_usd = 0);

ALTER TABLE operations
    ADD COLUMN reserved_micro_usd bigint NOT NULL DEFAULT 0 CHECK (reserved_micro_usd >= 0),
    ADD COLUMN settled_micro_usd bigint CHECK (settled_micro_usd >= 0),
    ADD CONSTRAINT operations_settled_only_when_closed CHECK (
        settled_micro_usd IS NULL OR state IN ('succeeded', 'failed', 'abandoned')
    );

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
    IF NEW.settled_micro_usd < OLD.settled_micro_usd THEN
        RAISE EXCEPTION 'settled spending of % cannot decrease', OLD.investigation_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.investigation_id, NEW.principal_id, NEW.idempotency_key, NEW.request_hash,
        NEW.workflow_version, NEW.prompt_version, NEW.max_llm_calls, NEW.max_tool_calls,
        NEW.max_output_tokens, NEW.deadline_at, NEW.created_at, NEW.budget_micro_usd,
        NEW.pricing)
        IS DISTINCT FROM
       (OLD.investigation_id, OLD.principal_id, OLD.idempotency_key, OLD.request_hash,
        OLD.workflow_version, OLD.prompt_version, OLD.max_llm_calls, OLD.max_tool_calls,
        OLD.max_output_tokens, OLD.deadline_at, OLD.created_at, OLD.budget_micro_usd,
        OLD.pricing) THEN
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
        NEW.started_at, NEW.reserved_micro_usd)
        IS DISTINCT FROM
       (OLD.operation_id, OLD.investigation_id, OLD.operation_type, OLD.attempt,
        OLD.fencing_token, OLD.model_alias, OLD.gateway_request_id, OLD.max_tokens,
        OLD.started_at, OLD.reserved_micro_usd) THEN
        RAISE EXCEPTION 'immutable fields of operation % cannot change', OLD.operation_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.state = OLD.state THEN
        -- The only change allowed without a transition: clearing a recorded reply.
        IF OLD.state = 'succeeded' AND NEW.reply IS NULL AND
           (NEW.may_have_executed, NEW.failure_code, NEW.gateway_status,
            NEW.gateway_error_code, NEW.usage, NEW.dispatched_at, NEW.completed_at,
            NEW.settled_micro_usd)
           IS NOT DISTINCT FROM
           (OLD.may_have_executed, OLD.failure_code, OLD.gateway_status,
            OLD.gateway_error_code, OLD.usage, OLD.dispatched_at, OLD.completed_at,
            OLD.settled_micro_usd) THEN
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
