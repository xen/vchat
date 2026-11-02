--
-- PostgreSQL database dump
--

\restrict hejw986K2m5dz2TMy9SQxEZ1pyGNgde4AmJL7hwB0wdVi8iL28MqIcQE4GC1jG2

-- Dumped from database version 18.6 (Homebrew)
-- Dumped by pg_dump version 18.6 (Homebrew)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: paradedb; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA paradedb;


--
-- Name: pg_search; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_search WITH SCHEMA paradedb;


--
-- Name: EXTENSION pg_search; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_search IS 'pg_search: Full text search for PostgreSQL using BM25';


--
-- Name: pgcrypto; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;


--
-- Name: EXTENSION pgcrypto; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


--
-- Name: chatrole; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.chatrole AS ENUM (
    'system',
    'user',
    'assistant'
);


--
-- Name: status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.status AS ENUM (
    'crawler',
    'parsing',
    'ready'
);


--
-- Name: promote_duplicate_chunk_on_delete(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.promote_duplicate_chunk_on_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
    DECLARE
        promoted_id integer;
    BEGIN
        IF OLD.is_duplicate = false
           AND OLD.text_hash IS NOT NULL
           AND OLD.page_id IS NOT NULL THEN
            SELECT id
            INTO promoted_id
            FROM chunk
            WHERE text_hash = OLD.text_hash
              AND is_duplicate = true
              AND page_id IS NOT NULL
              AND id <> OLD.id
            ORDER BY id
            LIMIT 1;

            IF promoted_id IS NOT NULL THEN
                UPDATE chunk
                SET
                    is_duplicate = false,
                    duplicate_of_chunk_id = NULL,
                    embedding = OLD.embedding
                WHERE id = promoted_id;

                UPDATE chunk
                SET duplicate_of_chunk_id = promoted_id
                WHERE text_hash = OLD.text_hash
                  AND is_duplicate = true
                  AND page_id IS NOT NULL
                  AND id <> promoted_id;
            END IF;
        END IF;

        RETURN OLD;
    END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: admin_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_event (
    id integer NOT NULL,
    user_id integer,
    user_email character varying(254) NOT NULL,
    ip_address character varying(64),
    event_name character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: admin_event_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.admin_event_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: admin_event_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.admin_event_id_seq OWNED BY public.admin_event.id;


--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: api_client; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_client (
    id integer NOT NULL,
    name character varying(128) NOT NULL,
    client_id character varying(64) NOT NULL,
    encrypted_secret text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone
);


--
-- Name: api_client_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.api_client_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: api_client_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.api_client_id_seq OWNED BY public.api_client.id;


--
-- Name: api_client_source; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_client_source (
    api_client_id integer NOT NULL,
    source_id integer NOT NULL
);


--
-- Name: chat; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat (
    id character varying(36) NOT NULL,
    title character varying(200) NOT NULL,
    user_uid character varying(256) NOT NULL,
    meta jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone
);


--
-- Name: chat_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chat_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chat_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chat_id_seq OWNED BY public.chat.id;


--
-- Name: chat_msg; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_msg (
    id integer NOT NULL,
    text text NOT NULL,
    full_context text NOT NULL,
    role public.chatrole NOT NULL,
    chat_id character varying(36) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    user_uid character varying(256) NOT NULL,
    used_chunks jsonb,
    tokens integer DEFAULT 0 NOT NULL,
    provider character varying(64),
    model character varying(128),
    vote boolean,
    guardrail_triggered boolean DEFAULT false NOT NULL,
    guardrail_stage character varying(16),
    guardrail_reasons character varying[],
    embedding public.vector(1024),
    text_hash character varying(64),
    CONSTRAINT ck_chat_msg_user_text_max_4000 CHECK (((role <> 'user'::public.chatrole) OR (char_length(text) <= 4000)))
);


--
-- Name: chat_msg_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chat_msg_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chat_msg_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chat_msg_id_seq OWNED BY public.chat_msg.id;


--
-- Name: chunk; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk (
    id integer NOT NULL,
    user_uid character varying(256) NOT NULL,
    page_id integer,
    chunk_ix integer NOT NULL,
    start_offset integer,
    end_offset integer,
    text text CONSTRAINT chunk_content_not_null NOT NULL,
    embedding public.vector(1024),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone,
    kind character varying(32) DEFAULT 'text'::character varying NOT NULL,
    header_text text,
    section_path text,
    entity_terms character varying[],
    token_count integer DEFAULT 0 NOT NULL,
    text_hash character varying(64),
    is_duplicate boolean DEFAULT false NOT NULL,
    duplicate_of_chunk_id integer,
    entity_terms_text text DEFAULT ''::text NOT NULL
);


--
-- Name: chunk_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chunk_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chunk_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chunk_id_seq OWNED BY public.chunk.id;


--
-- Name: crawl_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.crawl_run (
    id integer NOT NULL,
    source_id integer NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    pages_crawled integer DEFAULT 0,
    pages_new integer DEFAULT 0,
    pages_changed integer DEFAULT 0,
    pages_errors integer DEFAULT 0,
    pages_excluded integer DEFAULT 0,
    was_rate_limited boolean DEFAULT false,
    exit_reason text,
    notes text
);


--
-- Name: crawl_run_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.crawl_run_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: crawl_run_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.crawl_run_id_seq OWNED BY public.crawl_run.id;


--
-- Name: page; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.page (
    id integer CONSTRAINT document_id_not_null NOT NULL,
    uri character varying,
    source_id integer,
    content text,
    hash character varying(64) CONSTRAINT document_hash_not_null NOT NULL,
    lang character varying(2) CONSTRAINT document_lang_not_null NOT NULL,
    length integer CONSTRAINT document_length_not_null NOT NULL,
    meta jsonb CONSTRAINT document_meta_not_null NOT NULL,
    title character varying,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP CONSTRAINT document_created_at_not_null NOT NULL,
    updated_at timestamp with time zone,
    http_status integer,
    last_crawled_at timestamp with time zone,
    last_etag text,
    last_modified_at timestamp with time zone,
    check_interval_days integer DEFAULT 7 CONSTRAINT document_check_interval_days_not_null NOT NULL,
    stable_count integer DEFAULT 0 CONSTRAINT document_stable_count_not_null NOT NULL,
    error_count integer DEFAULT 0 CONSTRAINT document_error_count_not_null NOT NULL,
    is_hub_page boolean DEFAULT false CONSTRAINT document_is_hub_page_not_null NOT NULL,
    content_value double precision,
    inlink_count integer DEFAULT 0 CONSTRAINT document_inlink_count_not_null NOT NULL,
    status_error character varying(64),
    status public.status DEFAULT 'crawler'::public.status CONSTRAINT page_status_new_not_null NOT NULL,
    has_triggers boolean DEFAULT false NOT NULL,
    triggers jsonb,
    raw_content bytea,
    raw_content_type text,
    raw_content_size integer,
    discovered_via character varying(32),
    discovered_from_url text,
    discover_by character varying(32),
    discover_source text,
    uri_slug text DEFAULT ''::text NOT NULL
);


--
-- Name: document_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.document_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: document_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.document_id_seq OWNED BY public.page.id;


--
-- Name: llm_cache_entry; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_cache_entry (
    id integer NOT NULL,
    purpose character varying(64) NOT NULL,
    cache_key character varying(64) NOT NULL,
    question_hash character varying(64) NOT NULL,
    retrieval_context_hash character varying(64),
    key_payload jsonb NOT NULL,
    response_payload jsonb NOT NULL,
    source_scope jsonb NOT NULL,
    provider character varying(64),
    model character varying(128),
    tokens integer DEFAULT 0 NOT NULL,
    observed_count integer DEFAULT 1 NOT NULL,
    potential_saved_requests integer DEFAULT 0 NOT NULL,
    potential_saved_tokens integer DEFAULT 0 NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL,
    disabled_reason character varying(255),
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: llm_cache_entry_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.llm_cache_entry_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: llm_cache_entry_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.llm_cache_entry_id_seq OWNED BY public.llm_cache_entry.id;


--
-- Name: page_link; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.page_link (
    id bigint NOT NULL,
    source_uri text NOT NULL,
    target_uri text NOT NULL,
    source_page_id integer,
    target_page_id integer,
    source_id integer,
    found_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: page_link_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.page_link_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: page_link_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.page_link_id_seq OWNED BY public.page_link.id;


--
-- Name: page_shingle; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.page_shingle (
    page_id integer NOT NULL,
    shingle_hash bigint NOT NULL,
    source_id integer NOT NULL
);


--
-- Name: request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.request (
    id integer NOT NULL,
    chat_id character varying(36) NOT NULL,
    status character varying(20) NOT NULL,
    email character varying NOT NULL,
    phone character varying NOT NULL,
    name character varying CONSTRAINT request_subject_not_null NOT NULL,
    body text CONSTRAINT request_text_not_null NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone,
    ip_address character varying(64),
    user_agent character varying(512)
);


--
-- Name: request_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.request_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: request_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.request_id_seq OWNED BY public.request.id;


--
-- Name: sitemap; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sitemap (
    id integer NOT NULL,
    source_id integer NOT NULL,
    url text NOT NULL,
    is_excluded boolean DEFAULT false NOT NULL,
    discovered_via character varying(32) DEFAULT 'manual'::character varying NOT NULL,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_fetched_at timestamp with time zone,
    last_etag text,
    last_content_hash text,
    url_count integer,
    discovered_from_url text,
    ignore_reason character varying(64)
);


--
-- Name: sitemap_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sitemap_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sitemap_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sitemap_id_seq OWNED BY public.sitemap.id;


--
-- Name: source; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.source (
    id integer NOT NULL,
    title character varying(255) NOT NULL,
    uri character varying NOT NULL,
    config jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone,
    last_reindexed_at timestamp with time zone,
    reindex_cron character varying(100) DEFAULT '0 3 * * 1'::character varying NOT NULL,
    robots_cache jsonb,
    is_paused boolean DEFAULT false NOT NULL,
    blocked_reason character varying(64),
    blocked_message text,
    blocked_checked_at timestamp with time zone,
    enable_triggers boolean DEFAULT false NOT NULL
);


--
-- Name: source_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.source_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: source_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.source_id_seq OWNED BY public.source.id;


--
-- Name: tmp_pg_search_probe; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tmp_pg_search_probe (
    id integer NOT NULL,
    title text,
    body text,
    source_id integer,
    is_duplicate boolean
);


--
-- Name: tmp_pg_search_probe2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tmp_pg_search_probe2 (
    id integer NOT NULL,
    title text,
    uri_slug text,
    entity_terms_text text,
    body text,
    kind text,
    source_id integer,
    is_duplicate boolean
);


--
-- Name: trigger_response_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trigger_response_cache (
    id integer NOT NULL,
    page_id integer NOT NULL,
    trigger_key character varying(64) NOT NULL,
    prompt_hash character varying(64) NOT NULL,
    response_text text NOT NULL,
    full_context text NOT NULL,
    used_chunks jsonb,
    provider character varying(64),
    model character varying(128),
    tokens integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone
);


--
-- Name: trigger_response_cache_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.trigger_response_cache_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: trigger_response_cache_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.trigger_response_cache_id_seq OWNED BY public.trigger_response_cache.id;


--
-- Name: user_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_session (
    id integer NOT NULL,
    user_id integer NOT NULL,
    session_id character varying(64) NOT NULL,
    ip_address character varying(128),
    user_agent character varying(512),
    last_seen_at timestamp with time zone,
    revoked_at timestamp with time zone,
    revoked_reason character varying(64),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone
);


--
-- Name: user_session_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.user_session_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: user_session_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.user_session_id_seq OWNED BY public.user_session.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id integer NOT NULL,
    email character varying NOT NULL,
    name character varying NOT NULL,
    password character varying,
    is_active boolean NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone,
    is_ldap boolean NOT NULL
);


--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: widget_integration; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.widget_integration (
    id integer NOT NULL,
    name character varying(128) NOT NULL,
    code character varying(64) NOT NULL,
    agent_name character varying(100) DEFAULT ''::character varying NOT NULL,
    system_prompt text DEFAULT ''::text NOT NULL,
    pinned_messages jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone,
    suggestions_enabled boolean DEFAULT true NOT NULL,
    suggestions_prompt text DEFAULT 'Ты генерируешь подсказки для продолжения диалога в чат-виджете.

Сгенерируй 2-3 коротких следующих вопроса или действия от лица пользователя.
Подсказки должны быть напрямую связаны с последним вопросом, финальным ответом ассистента и использованными источниками.
Не повторяй уже отвеченный вопрос. Не придумывай факты, которых нет в ответе или источниках.
Пиши на языке последнего вопроса пользователя.

Верни только JSON-объект:
{"actions": ["Короткая подсказка", "Короткая подсказка"]}

Последний вопрос пользователя:
{{user_question}}

Финальный ответ ассистента:
{{assistant_answer}}

Использованные источники:
{{sources}}
'::text NOT NULL,
    footer_text text DEFAULT '<a href="https://vbudushee.ru/faq/">Пользовательское соглашение</a>.<br>Отправить Enter, новая строка Shift+Enter'::text NOT NULL,
    welcome_messages jsonb DEFAULT '[]'::jsonb NOT NULL,
    waiting_messages jsonb DEFAULT '["Готовлю ответ"]'::jsonb NOT NULL,
    error_message text NOT NULL,
    is_enabled boolean DEFAULT true NOT NULL,
    secret character varying(128) NOT NULL,
    trigger_templates jsonb CONSTRAINT widget_integration_default_trigger_templates_not_null NOT NULL
);


--
-- Name: widget_integration_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.widget_integration_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: widget_integration_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.widget_integration_id_seq OWNED BY public.widget_integration.id;


--
-- Name: admin_event id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_event ALTER COLUMN id SET DEFAULT nextval('public.admin_event_id_seq'::regclass);


--
-- Name: api_client id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_client ALTER COLUMN id SET DEFAULT nextval('public.api_client_id_seq'::regclass);


--
-- Name: chat id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat ALTER COLUMN id SET DEFAULT nextval('public.chat_id_seq'::regclass);


--
-- Name: chat_msg id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_msg ALTER COLUMN id SET DEFAULT nextval('public.chat_msg_id_seq'::regclass);


--
-- Name: chunk id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk ALTER COLUMN id SET DEFAULT nextval('public.chunk_id_seq'::regclass);


--
-- Name: crawl_run id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crawl_run ALTER COLUMN id SET DEFAULT nextval('public.crawl_run_id_seq'::regclass);


--
-- Name: llm_cache_entry id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_cache_entry ALTER COLUMN id SET DEFAULT nextval('public.llm_cache_entry_id_seq'::regclass);


--
-- Name: page id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page ALTER COLUMN id SET DEFAULT nextval('public.document_id_seq'::regclass);


--
-- Name: page_link id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page_link ALTER COLUMN id SET DEFAULT nextval('public.page_link_id_seq'::regclass);


--
-- Name: request id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.request ALTER COLUMN id SET DEFAULT nextval('public.request_id_seq'::regclass);


--
-- Name: sitemap id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sitemap ALTER COLUMN id SET DEFAULT nextval('public.sitemap_id_seq'::regclass);


--
-- Name: source id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source ALTER COLUMN id SET DEFAULT nextval('public.source_id_seq'::regclass);


--
-- Name: trigger_response_cache id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_response_cache ALTER COLUMN id SET DEFAULT nextval('public.trigger_response_cache_id_seq'::regclass);


--
-- Name: user_session id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_session ALTER COLUMN id SET DEFAULT nextval('public.user_session_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: widget_integration id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.widget_integration ALTER COLUMN id SET DEFAULT nextval('public.widget_integration_id_seq'::regclass);


--
-- Name: admin_event admin_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_event
    ADD CONSTRAINT admin_event_pkey PRIMARY KEY (id);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: api_client api_client_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_client
    ADD CONSTRAINT api_client_pkey PRIMARY KEY (id);


--
-- Name: api_client_source api_client_source_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_client_source
    ADD CONSTRAINT api_client_source_pkey PRIMARY KEY (api_client_id, source_id);


--
-- Name: chat_msg chat_msg_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_msg
    ADD CONSTRAINT chat_msg_pkey PRIMARY KEY (id);


--
-- Name: chat chat_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat
    ADD CONSTRAINT chat_pkey PRIMARY KEY (id);


--
-- Name: chunk chunk_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk
    ADD CONSTRAINT chunk_pkey PRIMARY KEY (id);


--
-- Name: crawl_run crawl_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crawl_run
    ADD CONSTRAINT crawl_run_pkey PRIMARY KEY (id);


--
-- Name: page document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page
    ADD CONSTRAINT document_pkey PRIMARY KEY (id);


--
-- Name: llm_cache_entry llm_cache_entry_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_cache_entry
    ADD CONSTRAINT llm_cache_entry_pkey PRIMARY KEY (id);


--
-- Name: page_link page_link_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page_link
    ADD CONSTRAINT page_link_pkey PRIMARY KEY (id);


--
-- Name: page_shingle page_shingle_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page_shingle
    ADD CONSTRAINT page_shingle_pkey PRIMARY KEY (page_id, shingle_hash);


--
-- Name: request request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.request
    ADD CONSTRAINT request_pkey PRIMARY KEY (id);


--
-- Name: sitemap sitemap_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sitemap
    ADD CONSTRAINT sitemap_pkey PRIMARY KEY (id);


--
-- Name: source source_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.source
    ADD CONSTRAINT source_pkey PRIMARY KEY (id);


--
-- Name: tmp_pg_search_probe2 tmp_pg_search_probe2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tmp_pg_search_probe2
    ADD CONSTRAINT tmp_pg_search_probe2_pkey PRIMARY KEY (id);


--
-- Name: tmp_pg_search_probe tmp_pg_search_probe_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tmp_pg_search_probe
    ADD CONSTRAINT tmp_pg_search_probe_pkey PRIMARY KEY (id);


--
-- Name: trigger_response_cache trigger_response_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_response_cache
    ADD CONSTRAINT trigger_response_cache_pkey PRIMARY KEY (id);


--
-- Name: llm_cache_entry uq_llm_cache_entry_purpose_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_cache_entry
    ADD CONSTRAINT uq_llm_cache_entry_purpose_key UNIQUE (purpose, cache_key);


--
-- Name: trigger_response_cache uq_trigger_response_cache_page_trigger_prompt; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_response_cache
    ADD CONSTRAINT uq_trigger_response_cache_page_trigger_prompt UNIQUE (page_id, trigger_key, prompt_hash);


--
-- Name: user_session user_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_session
    ADD CONSTRAINT user_session_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: widget_integration widget_integration_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.widget_integration
    ADD CONSTRAINT widget_integration_pkey PRIMARY KEY (id);


--
-- Name: ix_admin_event_event_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_admin_event_event_name ON public.admin_event USING btree (event_name);


--
-- Name: ix_admin_event_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_admin_event_user_id ON public.admin_event USING btree (user_id);


--
-- Name: ix_api_client_client_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_api_client_client_id ON public.api_client USING btree (client_id);


--
-- Name: ix_api_client_source_source_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_client_source_source_id ON public.api_client_source USING btree (source_id);


--
-- Name: ix_chat_msg_chat_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_msg_chat_id ON public.chat_msg USING btree (chat_id);


--
-- Name: ix_chat_msg_guardrail_triggered; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_msg_guardrail_triggered ON public.chat_msg USING btree (guardrail_triggered);


--
-- Name: ix_chat_msg_text_fts_simple; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_msg_text_fts_simple ON public.chat_msg USING gin (to_tsvector('simple'::regconfig, COALESCE(text, ''::text))) WHERE (role = ANY (ARRAY['user'::public.chatrole, 'assistant'::public.chatrole]));


--
-- Name: ix_chat_msg_text_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_msg_text_hash ON public.chat_msg USING btree (text_hash);


--
-- Name: ix_chat_msg_user_uid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_msg_user_uid ON public.chat_msg USING btree (user_uid);


--
-- Name: ix_chat_search_fts_simple; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_search_fts_simple ON public.chat USING gin (to_tsvector('simple'::regconfig, (((COALESCE(title, ''::character varying))::text || ' '::text) || (COALESCE(user_uid, ''::character varying))::text)));


--
-- Name: ix_chat_user_uid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chat_user_uid ON public.chat USING btree (user_uid);


--
-- Name: ix_chunk_bm25; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chunk_bm25 ON public.chunk USING bm25 (id, ((header_text)::pdb.simple('alias=header')), ((section_path)::pdb.simple('alias=section')), ((entity_terms_text)::pdb.simple('alias=entities')), ((text)::pdb.simple('alias=body')), ((kind)::pdb.literal('alias=kind')), page_id, is_duplicate) WITH (key_field=id);


--
-- Name: ix_chunk_document_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chunk_document_id ON public.chunk USING btree (page_id);


--
-- Name: ix_chunk_document_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chunk_document_kind ON public.chunk USING btree (page_id, kind);


--
-- Name: ix_chunk_duplicate_of_chunk_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chunk_duplicate_of_chunk_id ON public.chunk USING btree (duplicate_of_chunk_id);


--
-- Name: ix_chunk_embedding_kb_hnsw_cosine; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chunk_embedding_kb_hnsw_cosine ON public.chunk USING hnsw (embedding public.vector_cosine_ops) WHERE ((page_id IS NOT NULL) AND (is_duplicate = false) AND (embedding IS NOT NULL));


--
-- Name: ix_chunk_is_duplicate; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chunk_is_duplicate ON public.chunk USING btree (is_duplicate);


--
-- Name: ix_chunk_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chunk_kind ON public.chunk USING btree (kind);


--
-- Name: ix_chunk_text_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chunk_text_hash ON public.chunk USING btree (text_hash);


--
-- Name: ix_chunk_user_uid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_chunk_user_uid ON public.chunk USING btree (user_uid);


--
-- Name: ix_document_source_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_source_id ON public.page USING btree (source_id);


--
-- Name: ix_llm_cache_entry_is_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_cache_entry_is_enabled ON public.llm_cache_entry USING btree (is_enabled);


--
-- Name: ix_llm_cache_entry_last_seen_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_cache_entry_last_seen_at ON public.llm_cache_entry USING btree (last_seen_at);


--
-- Name: ix_llm_cache_entry_purpose; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_cache_entry_purpose ON public.llm_cache_entry USING btree (purpose);


--
-- Name: ix_llm_cache_entry_question_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_cache_entry_question_hash ON public.llm_cache_entry USING btree (question_hash);


--
-- Name: ix_llm_cache_entry_retrieval_context_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_llm_cache_entry_retrieval_context_hash ON public.llm_cache_entry USING btree (retrieval_context_hash);


--
-- Name: ix_page_bm25; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_page_bm25 ON public.page USING bm25 (id, ((title)::pdb.simple('alias=title')), ((uri_slug)::pdb.ngram('3', '5', 'alias=uri_slug')), ((content)::pdb.simple('alias=body')), source_id, status_error, content_value) WITH (key_field=id);


--
-- Name: ix_page_discover_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_page_discover_by ON public.page USING btree (discover_by);


--
-- Name: ix_page_discovered_via; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_page_discovered_via ON public.page USING btree (discovered_via);


--
-- Name: ix_page_has_triggers; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_page_has_triggers ON public.page USING btree (has_triggers);


--
-- Name: ix_page_link_source_page_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_page_link_source_page_id ON public.page_link USING btree (source_page_id);


--
-- Name: ix_page_link_target_page_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_page_link_target_page_id ON public.page_link USING btree (target_page_id);


--
-- Name: ix_page_shingle_source_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_page_shingle_source_hash ON public.page_shingle USING btree (source_id, shingle_hash);


--
-- Name: ix_page_shingle_source_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_page_shingle_source_id ON public.page_shingle USING btree (source_id);


--
-- Name: ix_page_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_page_status ON public.page USING btree (status);


--
-- Name: ix_page_status_error; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_page_status_error ON public.page USING btree (status_error);


--
-- Name: ix_source_is_paused; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_source_is_paused ON public.source USING btree (is_paused);


--
-- Name: ix_trigger_response_cache_page_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_trigger_response_cache_page_id ON public.trigger_response_cache USING btree (page_id);


--
-- Name: ix_user_session_token; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_user_session_token ON public.user_session USING btree (session_id);


--
-- Name: ix_user_session_user_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_session_user_active ON public.user_session USING btree (user_id, revoked_at);


--
-- Name: ix_users_email; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);


--
-- Name: ix_widget_integration_code; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_widget_integration_code ON public.widget_integration USING btree (code);


--
-- Name: tmp_pg_search_probe2_bm25; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tmp_pg_search_probe2_bm25 ON public.tmp_pg_search_probe2 USING bm25 (id, ((title)::pdb.simple('alias=title')), ((uri_slug)::pdb.ngram('3', '5', 'alias=uri_slug')), ((entity_terms_text)::pdb.simple('alias=entities')), ((body)::pdb.simple('alias=body')), ((kind)::pdb.literal('alias=kind')), source_id, is_duplicate) WITH (key_field=id);


--
-- Name: tmp_pg_search_probe_bm25; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tmp_pg_search_probe_bm25 ON public.tmp_pg_search_probe USING bm25 (id, title, body, source_id, is_duplicate) WITH (key_field=id);


--
-- Name: uq_page_uri; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_page_uri ON public.page USING btree (uri);


--
-- Name: chunk trigger_promote_duplicate_chunk_on_delete; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trigger_promote_duplicate_chunk_on_delete BEFORE DELETE ON public.chunk FOR EACH ROW EXECUTE FUNCTION public.promote_duplicate_chunk_on_delete();


--
-- Name: admin_event admin_event_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_event
    ADD CONSTRAINT admin_event_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: api_client_source api_client_source_api_client_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_client_source
    ADD CONSTRAINT api_client_source_api_client_id_fkey FOREIGN KEY (api_client_id) REFERENCES public.api_client(id) ON DELETE CASCADE;


--
-- Name: api_client_source api_client_source_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_client_source
    ADD CONSTRAINT api_client_source_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id) ON DELETE CASCADE;


--
-- Name: chunk chunk_page_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk
    ADD CONSTRAINT chunk_page_id_fkey FOREIGN KEY (page_id) REFERENCES public.page(id) ON DELETE CASCADE;


--
-- Name: crawl_run crawl_run_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.crawl_run
    ADD CONSTRAINT crawl_run_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id) ON DELETE CASCADE;


--
-- Name: page document_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page
    ADD CONSTRAINT document_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id) ON DELETE CASCADE;


--
-- Name: chat_msg fk_chat_msg_chat_id_chat; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_msg
    ADD CONSTRAINT fk_chat_msg_chat_id_chat FOREIGN KEY (chat_id) REFERENCES public.chat(id) ON DELETE CASCADE;


--
-- Name: request fk_request_chat_id_chat; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.request
    ADD CONSTRAINT fk_request_chat_id_chat FOREIGN KEY (chat_id) REFERENCES public.chat(id);


--
-- Name: page_link page_link_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page_link
    ADD CONSTRAINT page_link_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id) ON DELETE CASCADE;


--
-- Name: page_link page_link_source_page_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page_link
    ADD CONSTRAINT page_link_source_page_id_fkey FOREIGN KEY (source_page_id) REFERENCES public.page(id) ON DELETE CASCADE;


--
-- Name: page_link page_link_target_page_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page_link
    ADD CONSTRAINT page_link_target_page_id_fkey FOREIGN KEY (target_page_id) REFERENCES public.page(id) ON DELETE CASCADE;


--
-- Name: page_shingle page_shingle_page_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page_shingle
    ADD CONSTRAINT page_shingle_page_id_fkey FOREIGN KEY (page_id) REFERENCES public.page(id) ON DELETE CASCADE;


--
-- Name: page_shingle page_shingle_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.page_shingle
    ADD CONSTRAINT page_shingle_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id) ON DELETE CASCADE;


--
-- Name: sitemap sitemap_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sitemap
    ADD CONSTRAINT sitemap_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id) ON DELETE CASCADE;


--
-- Name: trigger_response_cache trigger_response_cache_page_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trigger_response_cache
    ADD CONSTRAINT trigger_response_cache_page_id_fkey FOREIGN KEY (page_id) REFERENCES public.page(id) ON DELETE CASCADE;


--
-- Name: user_session user_session_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_session
    ADD CONSTRAINT user_session_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict hejw986K2m5dz2TMy9SQxEZ1pyGNgde4AmJL7hwB0wdVi8iL28MqIcQE4GC1jG2

