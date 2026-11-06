-- Investigation registry and model call journal. See docs/agent-durable-execution.md.
-- Runs with search_path set to the agent schema.

CREATE TABLE investigations (
    investigation_id text PRIMARY KEY CHECK (investigation_id ~ '^inv-[0-9a-f]{32}$'),
    principal_id text NOT NULL CHECK (principal_id ~ '^[a-z0-9-]{1,64}$'),
    idempotency_key text CHECK (idempotency_key ~ '^[A-Za-z0-9._:-]{1,128}$'),
    request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
    -- Needed to resume; cleared when the investigation becomes terminal.
    incident text CHECK (char_length(incident) BETWEEN 1 AND 4000),
    status text NOT NULL DEFAULT 'accepted' CHECK (
        status IN ('accepted', 'running', 'completed', 'failed', 'reconciliation_required')
    ),
    current_stage text CHECK (current_stage IN (
        'plan', 'validate_plan', 'collect_diagnostics', 'diagnose', 'validate_diagnosis',
        'finalize'
    )),
    workflow_version text NOT NULL CHECK (char_length(workflow_version) BETWEEN 1 AND 64),
    prompt_version text NOT NULL CHECK (char_length(prompt_version) BETWEEN 1 AND 64),
    max_llm_calls integer NOT NULL CHECK (max_llm_calls BETWEEN 1 AND 8),
    max_tool_calls integer NOT NULL CHECK (max_tool_calls BETWEEN 1 AND 16),
    max_output_tokens integer NOT NULL CHECK (max_output_tokens BETWEEN 64 AND 16384),
    llm_calls_admitted integer NOT NULL DEFAULT 0
        CHECK (llm_calls_admitted >= 0 AND llm_calls_admitted <= max_llm_calls),
    tool_calls_admitted integer NOT NULL DEFAULT 0
        CHECK (tool_calls_admitted >= 0 AND tool_calls_admitted <= max_tool_calls),
    deadline_at timestamptz NOT NULL,
    lease_owner text CHECK (char_length(lease_owner) BETWEEN 1 AND 128),
    lease_expires_at timestamptz,
    fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    failure_code text CHECK (failure_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    result jsonb CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT investigations_idempotency_key UNIQUE (principal_id, idempotency_key),
    CONSTRAINT investigations_lease_pair
        CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
    CONSTRAINT investigations_lease_only_while_running
        CHECK (lease_owner IS NULL OR status = 'running'),
    CONSTRAINT investigations_accepted_is_unclaimed
        CHECK (status <> 'accepted' OR (fencing_token = 0 AND attempts = 0)),
    CONSTRAINT investigations_terminal_completed_at CHECK (
        (status IN ('completed', 'failed', 'reconciliation_required')) = (completed_at IS NOT NULL)
    ),
    CONSTRAINT investigations_incident_until_terminal CHECK (
        (status IN ('completed', 'failed', 'reconciliation_required')) = (incident IS NULL)
    ),
    CONSTRAINT investigations_terminal_result CHECK (
        status NOT IN ('completed', 'failed', 'reconciliation_required') OR result IS NOT NULL
    ),
    CONSTRAINT investigations_failure_code CHECK (
        (status IN ('failed', 'reconciliation_required')) = (failure_code IS NOT NULL)
    )
);

CREATE INDEX investigations_claimable ON investigations (created_at)
    WHERE status IN ('accepted', 'running');
CREATE INDEX investigations_active_by_principal ON investigations (principal_id)
    WHERE status IN ('accepted', 'running');

CREATE FUNCTION investigations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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

CREATE TRIGGER investigations_guard BEFORE UPDATE ON investigations
    FOR EACH ROW EXECUTE FUNCTION investigations_guard();

CREATE TABLE operations (
    operation_id uuid PRIMARY KEY,
    investigation_id text NOT NULL REFERENCES investigations (investigation_id),
    operation_type text NOT NULL CHECK (operation_type IN ('plan', 'diagnose')),
    attempt integer NOT NULL CHECK (attempt BETWEEN 1 AND 8),
    state text NOT NULL CHECK (
        state IN ('intent', 'dispatched', 'succeeded', 'failed', 'uncertain', 'abandoned')
    ),
    -- The fencing token of the owner that created the operation.
    fencing_token bigint NOT NULL CHECK (fencing_token >= 1),
    model_alias text NOT NULL CHECK (model_alias ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'),
    gateway_request_id text NOT NULL UNIQUE
        CHECK (gateway_request_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$'),
    max_tokens integer NOT NULL CHECK (max_tokens >= 1),
    may_have_executed boolean,
    failure_code text CHECK (failure_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    gateway_status integer CHECK (gateway_status BETWEEN 100 AND 599),
    gateway_error_code text CHECK (gateway_error_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    usage jsonb CHECK (usage IS NULL OR jsonb_typeof(usage) = 'object'),
    -- The model's reply, so a resumed worker never needs to call again. Cleared
    -- when the investigation becomes terminal.
    reply text CHECK (octet_length(reply) <= 4194304),
    started_at timestamptz NOT NULL DEFAULT now(),
    dispatched_at timestamptz,
    completed_at timestamptz,
    CONSTRAINT operations_one_attempt UNIQUE (investigation_id, operation_type, attempt),
    CONSTRAINT operations_dispatched_at CHECK (
        (state IN ('dispatched', 'succeeded', 'failed', 'uncertain')) = (dispatched_at IS NOT NULL)
    ),
    CONSTRAINT operations_completed_at CHECK (
        (state IN ('succeeded', 'failed', 'uncertain', 'abandoned')) = (completed_at IS NOT NULL)
    ),
    CONSTRAINT operations_reply_only_on_success CHECK (reply IS NULL OR state = 'succeeded'),
    CONSTRAINT operations_failure_fields CHECK (
        state <> 'failed' OR (failure_code IS NOT NULL AND may_have_executed IS NOT NULL)
    ),
    CONSTRAINT operations_unexecuted_states CHECK (
        state NOT IN ('intent', 'abandoned') OR may_have_executed IS NOT TRUE
    )
);

-- At most one model call in progress per investigation.
CREATE UNIQUE INDEX operations_one_open ON operations (investigation_id)
    WHERE state IN ('intent', 'dispatched');

CREATE FUNCTION operations_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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

CREATE TRIGGER operations_guard BEFORE UPDATE ON operations
    FOR EACH ROW EXECUTE FUNCTION operations_guard();
