-- LEGION V28 schema. Stored in the isolated legion_v28 PostgreSQL schema so the current version remains untouched.
CREATE TABLE IF NOT EXISTS employees (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    metro TEXT NOT NULL DEFAULT '',
    height_cm INTEGER,
    phone TEXT NOT NULL DEFAULT '',
    telegram_username TEXT NOT NULL DEFAULT '',
    has_car BOOLEAN NOT NULL DEFAULT FALSE,
    employee_group TEXT NOT NULL DEFAULT 'reserve' CHECK (employee_group IN ('brigadier','main','cashless','reserve')),
    uniform TEXT NOT NULL DEFAULT '',
    experienced BOOLEAN NOT NULL DEFAULT FALSE,
    remarks_count INTEGER NOT NULL DEFAULT 0,
    is_new BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    paused BOOLEAN NOT NULL DEFAULT FALSE,
    can_cash BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS readiness_current (
    id BIGSERIAL PRIMARY KEY,
    employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    work_date DATE NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ready','ready_car','main','off')),
    raw_text TEXT NOT NULL DEFAULT '',
    height_reported INTEGER,
    has_car_reported BOOLEAN,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(employee_id, work_date)
);

CREATE TABLE IF NOT EXISTS readiness_history (
    id BIGSERIAL PRIMARY KEY,
    employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    work_date DATE NOT NULL,
    previous_status TEXT,
    new_status TEXT NOT NULL,
    raw_text TEXT NOT NULL DEFAULT '',
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agents (
    id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    full_name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    work_date DATE NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('private','gbu')),
    daily_number INTEGER NOT NULL,
    agent_telegram_id BIGINT,
    agent_name TEXT NOT NULL DEFAULT '',
    agent_phone TEXT NOT NULL DEFAULT '',
    deceased_name TEXT NOT NULL DEFAULT '',
    issue_time TIME NOT NULL,
    contact_time TIME,
    route TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'standard' CHECK (category IN ('standard','vip','elite')),
    people_count INTEGER NOT NULL CHECK (people_count IN (4,6)),
    target_height INTEGER,
    required_uniform TEXT NOT NULL DEFAULT '',
    other_agent_name TEXT NOT NULL DEFAULT '',
    other_agent_phone TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    requires_cash BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','staffing','ready','sent','in_progress','needs_review','completed','cancelled')),
    created_by BIGINT,
    updated_by BIGINT,
    brigadier_sent_at TIMESTAMPTZ,
    agent_contact_sent_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    tips_amount NUMERIC(12,2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(work_date, source, daily_number)
);

CREATE INDEX IF NOT EXISTS idx_orders_date_source ON orders(work_date, source, issue_time);
CREATE INDEX IF NOT EXISTS idx_orders_agent ON orders(agent_telegram_id, work_date DESC);

CREATE TABLE IF NOT EXISTS assignments (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('brigadier','member')),
    assigned_by BIGINT,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accepted_at TIMESTAMPTZ,
    contact_confirmed_at TIMESTAMPTZ,
    UNIQUE(order_id, employee_id)
);

CREATE INDEX IF NOT EXISTS idx_assignments_employee ON assignments(employee_id, order_id);

CREATE TABLE IF NOT EXISTS brigadier_preferences (
    brigadier_employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    priority INTEGER NOT NULL DEFAULT 100,
    PRIMARY KEY (brigadier_employee_id, employee_id)
);

CREATE TABLE IF NOT EXISTS photos (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN ('private','gbu_first','gbu_second')),
    telegram_file_id TEXT NOT NULL,
    telegram_message_id BIGINT,
    reply_to_message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cash_entries (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT UNIQUE NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    brigadier_employee_id BIGINT REFERENCES employees(id) ON DELETE SET NULL,
    amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    employee_count SMALLINT CHECK (employee_count BETWEEN 2 AND 8),
    rollback NUMERIC(12,2) NOT NULL DEFAULT 0,
    reserve NUMERIC(12,2) NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    period_locked BOOLEAN NOT NULL DEFAULT FALSE,
    updated_by BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


ALTER TABLE cash_entries
    ADD COLUMN IF NOT EXISTS employee_count SMALLINT CHECK (employee_count BETWEEN 2 AND 8);

CREATE TABLE IF NOT EXISTS salary_rates (
    id BIGSERIAL PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS salary_adjustments (
    id BIGSERIAL PRIMARY KEY,
    employee_id BIGINT NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    adjustment_date DATE NOT NULL,
    amount NUMERIC(12,2) NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other',
    note TEXT NOT NULL DEFAULT '',
    created_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS notification_state (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT REFERENCES orders(id) ON DELETE CASCADE,
    employee_id BIGINT REFERENCES employees(id) ON DELETE CASCADE,
    notification_key TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(order_id, employee_id, notification_key)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    actor_telegram_id BIGINT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS photo_report_deliveries (
    id BIGSERIAL PRIMARY KEY,
    photo_id BIGINT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    delivery_kind TEXT NOT NULL DEFAULT 'preview' CHECK (delivery_kind IN ('preview','final')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(photo_id, chat_id, message_id)
);
