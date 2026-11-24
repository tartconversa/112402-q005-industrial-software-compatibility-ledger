-- 工业设备升级跟踪系统数据库结构（SQLite）。
-- 设计原则：
--   1. 所有外部事实（快照、正文、附件哈希、来源状态）不可原地覆盖，只追加新版本；
--   2. 人工确认（身份、兼容、例外、回执）都带批准人与时间戳；
--   3. 已经执行的升级通过 execution_receipts 固化当时采用的构建与批准证据，
--      后续重算不得改写历史。

PRAGMA foreign_keys = ON;

-- 通用元数据（schema 版本、初始化时间等）
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 操作人：资产管理员 / 产线工程师 / 班组长 / 工程师
CREATE TABLE IF NOT EXISTS actors (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('asset_admin', 'line_engineer', 'team_lead', 'engineer')),
    created_at TEXT NOT NULL
);

-- 外部来源登记（支持门户、下载页、邮件公告导出、授权设备清单）
CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('catalog', 'advisory', 'inventory')),
    url           TEXT NOT NULL,
    parser        TEXT NOT NULL,
    credentials   TEXT,                       -- 凭据引用名，绝不存明文
    enabled       INTEGER NOT NULL DEFAULT 1,
    etag          TEXT,                       -- 增量抓取用的校验标记
    last_modified TEXT,
    config_json   TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 每次抓取尝试一条记录，失败不等于“没有新版本”
CREATE TABLE IF NOT EXISTS fetch_runs (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES sources(id),
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    outcome         TEXT NOT NULL CHECK (outcome IN ('ok', 'not_modified', 'http_error', 'network_error', 'parse_error', 'empty_suspect')),
    http_status     INTEGER,
    error_message   TEXT,
    bytes_received  INTEGER,
    content_sha256  TEXT,
    raw_snapshot_id TEXT REFERENCES raw_snapshots(id)
);
CREATE INDEX IF NOT EXISTS idx_fetch_runs_source ON fetch_runs(source_id, started_at);

-- 原始响应快照（字节原文 + 哈希），正文版本可追溯的底座
CREATE TABLE IF NOT EXISTS raw_snapshots (
    id            TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL REFERENCES sources(id),
    fetched_at    TEXT NOT NULL,
    content_type  TEXT,
    body          BLOB NOT NULL,
    sha256        TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    etag          TEXT,
    last_modified TEXT
);

-- 厂商
CREATE TABLE IF NOT EXISTS vendors (
    id         TEXT PRIMARY KEY,            -- 名称规范化后的稳定标识
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- 产品（厂商命名空间内的产品名）
CREATE TABLE IF NOT EXISTS products (
    id         TEXT PRIMARY KEY,
    vendor_id  TEXT NOT NULL REFERENCES vendors(id),
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (vendor_id, name)
);

-- 产品构建：版本号相同但构建号/渠道不同即不同记录，绝不自动等价
CREATE TABLE IF NOT EXISTS builds (
    id           TEXT PRIMARY KEY,
    product_id   TEXT NOT NULL REFERENCES products(id),
    version      TEXT NOT NULL,            -- 厂商原文版本号
    build_number TEXT NOT NULL,            -- 厂商原文构建号（可能与版本号相同）
    display_name TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (product_id, version, build_number)
);

-- 来源中出现的发布条目（同一构建在 stable/regional 等不同渠道是不同发布）
CREATE TABLE IF NOT EXISTS releases (
    id              TEXT PRIMARY KEY,       -- 来源原文 release_id，加来源前缀防串
    source_id       TEXT NOT NULL REFERENCES sources(id),
    build_id        TEXT REFERENCES builds(id),
    product_hint    TEXT NOT NULL,          -- 解析时未确认身份前的原文产品名
    vendor_hint     TEXT NOT NULL,
    channel         TEXT NOT NULL,
    posted_at       TEXT,
    state           TEXT NOT NULL DEFAULT 'active'
                    CHECK (state IN ('active', 'withdrawn', 'yanked')),
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    state_changed_at TEXT,
    UNIQUE (source_id, id)
);
CREATE INDEX IF NOT EXISTS idx_releases_build ON releases(build_id);
CREATE INDEX IF NOT EXISTS idx_releases_state ON releases(state);

-- 发布附件（下载物）及其校验信息
CREATE TABLE IF NOT EXISTS release_artifacts (
    id                  TEXT PRIMARY KEY,
    release_id          TEXT NOT NULL REFERENCES releases(id),
    file_name           TEXT NOT NULL,
    download_url        TEXT NOT NULL,
    size_bytes          INTEGER,
    checksum_algorithm  TEXT,
    checksum_value      TEXT,
    observed_sha256     TEXT,               -- 实际下载后计算的哈希（可能尚未下载）
    first_seen_at       TEXT NOT NULL,
    UNIQUE (release_id, file_name, download_url)
);

-- 公告（厂商安全公告/升级通知/工程师补录的离线公告）
CREATE TABLE IF NOT EXISTS announcements (
    id              TEXT PRIMARY KEY,
    source_id       TEXT REFERENCES sources(id),  -- 补录公告可能无来源
    manual_entry    INTEGER NOT NULL DEFAULT 0,
    external_key    TEXT NOT NULL,                  -- 公告原文编号
    title           TEXT NOT NULL,
    severity        TEXT,
    published_at    TEXT,
    withdrawn       INTEGER NOT NULL DEFAULT 0,
    withdrawn_at    TEXT,
    current_revision_id TEXT REFERENCES announcement_revisions(id),
    created_at      TEXT NOT NULL,
    UNIQUE (source_id, external_key)
);
CREATE INDEX IF NOT EXISTS idx_announcements_published ON announcements(published_at);

-- 公告正文版本：正文每次变化追加一行，哈希定址，不覆盖旧版本
CREATE TABLE IF NOT EXISTS announcement_revisions (
    id               TEXT PRIMARY KEY,
    announcement_id  TEXT NOT NULL REFERENCES announcements(id),
    revision_no      INTEGER NOT NULL,
    body_text        TEXT NOT NULL,
    body_sha256      TEXT NOT NULL,
    content_url      TEXT,
    fetched_run_id   TEXT REFERENCES fetch_runs(id),
    created_at       TEXT NOT NULL,
    created_by       TEXT REFERENCES actors(id),    -- 补录时记录工程师
    UNIQUE (announcement_id, revision_no)
);

-- 公告附件（含厂商给出的校验值与实际观测哈希）
CREATE TABLE IF NOT EXISTS announcement_artifacts (
    id                  TEXT PRIMARY KEY,
    revision_id         TEXT NOT NULL REFERENCES announcement_revisions(id),
    file_name           TEXT NOT NULL,
    download_url        TEXT,
    size_bytes          INTEGER,
    checksum_algorithm  TEXT,
    checksum_value      TEXT,
    observed_sha256     TEXT,
    first_seen_at       TEXT NOT NULL
);

-- 公告适用范围（产品/构建由后续身份确认逐步落定；raw_builds 保留原文）
CREATE TABLE IF NOT EXISTS announcement_targets (
    id               TEXT PRIMARY KEY,
    announcement_id  TEXT NOT NULL REFERENCES announcements(id),
    revision_id      TEXT NOT NULL REFERENCES announcement_revisions(id),
    product_id       TEXT REFERENCES products(id),
    product_hint     TEXT NOT NULL,
    raw_builds       TEXT NOT NULL,           -- JSON 数组，原文构建范围
    vendor_hint      TEXT NOT NULL,
    UNIQUE (revision_id, product_hint)
);

-- 授权设备清单中的设备
CREATE TABLE IF NOT EXISTS devices (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    line_id          TEXT NOT NULL,          -- 产线标识
    product_id       TEXT REFERENCES products(id),
    product_hint     TEXT NOT NULL,
    current_build_id TEXT REFERENCES builds(id),
    current_build_raw TEXT NOT NULL,         -- 清单原文构建，身份未确认时仍可保留
    inventory_source_id TEXT REFERENCES sources(id),
    updated_at       TEXT NOT NULL,
    UNIQUE (inventory_source_id, id)
);
CREATE INDEX IF NOT EXISTS idx_devices_line ON devices(line_id);

-- 设备清单构建变化事件（当前构建如何变迁的审计链）
CREATE TABLE IF NOT EXISTS inventory_events (
    id               TEXT PRIMARY KEY,
    device_id        TEXT NOT NULL REFERENCES devices(id),
    source_id        TEXT REFERENCES sources(id),
    old_build_raw    TEXT,
    new_build_raw    TEXT NOT NULL,
    raw_snapshot_id  TEXT REFERENCES raw_snapshots(id),
    occurred_at      TEXT NOT NULL
);

-- 构建身份确认：资产管理员把“原文构建”与产品-构建实体绑定或驳回
CREATE TABLE IF NOT EXISTS build_identities (
    id              TEXT PRIMARY KEY,
    product_hint    TEXT NOT NULL,
    vendor_hint     TEXT NOT NULL,
    build_raw       TEXT NOT NULL,
    product_id      TEXT REFERENCES products(id),
    build_id        TEXT REFERENCES builds(id),
    status          TEXT NOT NULL DEFAULT 'raw'
                    CHECK (status IN ('raw', 'confirmed', 'rejected')),
    decided_by      TEXT REFERENCES actors(id),
    decided_at      TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (vendor_hint, product_hint, build_raw)
);

-- 兼容矩阵：产线工程师确认某发布对某设备型号/产品可升级
CREATE TABLE IF NOT EXISTS compatibility (
    id                TEXT PRIMARY KEY,
    release_id        TEXT NOT NULL REFERENCES releases(id),
    product_id        TEXT NOT NULL REFERENCES products(id),
    decision          TEXT NOT NULL CHECK (decision IN ('compatible', 'incompatible', 'pending')),
    decided_by        TEXT REFERENCES actors(id),
    decided_at        TEXT,
    note              TEXT,
    supersedes_id     TEXT REFERENCES compatibility(id),  -- 纠正旧结论
    created_at        TEXT NOT NULL,
    UNIQUE (release_id, product_id, supersedes_id)
);
CREATE INDEX IF NOT EXISTS idx_compat_lookup ON compatibility(release_id, product_id);

-- 允许停机窗口
CREATE TABLE IF NOT EXISTS maintenance_windows (
    id          TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL REFERENCES devices(id),
    starts_on   TEXT NOT NULL,           -- YYYY-MM-DD
    ends_on     TEXT NOT NULL,
    note        TEXT,
    created_by  TEXT REFERENCES actors(id),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_windows_device ON maintenance_windows(device_id, starts_on);

-- 生产例外（暂缓升级）：仅对指定设备与期限有效，到期自动失效
CREATE TABLE IF NOT EXISTS production_exceptions (
    id            TEXT PRIMARY KEY,
    device_id     TEXT NOT NULL REFERENCES devices(id),
    starts_on     TEXT NOT NULL,
    ends_on       TEXT NOT NULL,
    reason        TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('pending', 'approved', 'rejected', 'expired', 'revoked')),
    decided_by    TEXT REFERENCES actors(id),
    decided_at    TEXT,
    created_at    TEXT NOT NULL,
    revoked_at    TEXT,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_exceptions_device ON production_exceptions(device_id, state);

-- 升级建议（设备 × 发布），重算时保留历史行
CREATE TABLE IF NOT EXISTS recommendations (
    id                   TEXT PRIMARY KEY,
    device_id            TEXT NOT NULL REFERENCES devices(id),
    release_id           TEXT NOT NULL REFERENCES releases(id),
    status               TEXT NOT NULL
                         CHECK (status IN ('actionable', 'needs_review', 'not_applicable')),
    reasons_json         TEXT NOT NULL,       -- 阻塞/复核原因列表
    linked_announcement_ids TEXT NOT NULL,    -- JSON 数组：证据公告
    inputs_hash          TEXT NOT NULL,       -- 输入快照哈希，变化才重算
    computed_at          TEXT NOT NULL,
    supersedes_id        TEXT REFERENCES recommendations(id)
);
CREATE INDEX IF NOT EXISTS idx_reco_current ON recommendations(device_id, release_id);

-- 建议重算历史（每次输入变化留痕，便于解释“为什么结论变了”）
CREATE TABLE IF NOT EXISTS recommendation_history (
    id                   TEXT PRIMARY KEY,
    device_id            TEXT NOT NULL,
    release_id           TEXT NOT NULL,
    status               TEXT NOT NULL,
    reasons_json         TEXT NOT NULL,
    linked_announcement_ids TEXT NOT NULL,
    inputs_hash          TEXT NOT NULL,
    computed_at          TEXT NOT NULL
);

-- 升级计划
CREATE TABLE IF NOT EXISTS upgrade_plans (
    id                  TEXT PRIMARY KEY,
    recommendation_id   TEXT NOT NULL REFERENCES recommendations(id),
    device_id           TEXT NOT NULL REFERENCES devices(id),
    release_id          TEXT NOT NULL REFERENCES releases(id),
    state               TEXT NOT NULL
                        CHECK (state IN ('blocked', 'scheduled', 'in_progress', 'done', 'canceled', 'superseded')),
    window_id           TEXT REFERENCES maintenance_windows(id),
    blocked_reason      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_device_state ON upgrade_plans(device_id, state);

-- 计划状态事件流
CREATE TABLE IF NOT EXISTS upgrade_plan_events (
    id          TEXT PRIMARY KEY,
    plan_id     TEXT NOT NULL REFERENCES upgrade_plans(id),
    event_type  TEXT NOT NULL,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    actor_id    TEXT REFERENCES actors(id),
    note        TEXT,
    occurred_at TEXT NOT NULL
);

-- 完成回执与工程师确认：固化执行时实际采用的构建与全部批准证据
CREATE TABLE IF NOT EXISTS execution_receipts (
    id                    TEXT PRIMARY KEY,
    plan_id               TEXT NOT NULL REFERENCES upgrade_plans(id),
    device_id             TEXT NOT NULL REFERENCES devices(id),
    target_release_id     TEXT NOT NULL,
    target_build_id       TEXT REFERENCES builds(id),
    target_build_raw      TEXT NOT NULL,        -- 执行时采用的构建原文
    submitted_by          TEXT REFERENCES actors(id),  -- 班组长
    submitted_at          TEXT,
    confirmed_by          TEXT REFERENCES actors(id),  -- 工程师
    confirmed_at          TEXT,
    result                TEXT CHECK (result IN ('success', 'failed', 'rolled_back')),
    engineer_note         TEXT,
    evidence_snapshot_json TEXT NOT NULL,       -- 当时建议、兼容、例外、公告证据快照
    created_at            TEXT NOT NULL
);

-- 待处理队列：抓取失败、格式变化、空结果存疑、人工待办都进这里
CREATE TABLE IF NOT EXISTS pending_queue (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN (
                      'fetch_failure', 'parse_failure', 'empty_suspect',
                      'identity_review', 'manual_review', 'recompute_review')),
    source_id     TEXT REFERENCES sources(id),
    fetch_run_id  TEXT REFERENCES fetch_runs(id),
    ref_type      TEXT,                         -- 关联实体类型
    ref_id        TEXT,                         -- 关联实体 id
    summary       TEXT NOT NULL,
    detail_json   TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'in_progress', 'resolved', 'ignored')),
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    resolved_by   TEXT REFERENCES actors(id),
    resolution    TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_open ON pending_queue(status, kind);
