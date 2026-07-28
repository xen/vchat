CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;



COMMENT ON EXTENSION pgcrypto IS 'cryptographic functions';



CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;



COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';



CREATE TYPE public.chatrole AS ENUM (
    'system',
    'user',
    'assistant'
);



CREATE TYPE public.status AS ENUM (
    'crawler',
    'parsing',
    'ready'
);



CREATE FUNCTION public.promote_duplicate_chunk_on_delete() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            promoted_id integer;
        BEGIN
            IF OLD.is_duplicate = false
               AND OLD.text_hash IS NOT NULL
               AND OLD.chat_id IS NULL
               AND OLD.page_id IS NOT NULL THEN
                SELECT id
                INTO promoted_id
                FROM chunk
                WHERE text_hash = OLD.text_hash
                  AND is_duplicate = true
                  AND chat_id IS NULL
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
                      AND chat_id IS NULL
                      AND page_id IS NOT NULL
                      AND id <> promoted_id;
                END IF;
            END IF;

            RETURN OLD;
        END;
        $$;



CREATE FUNCTION public.refresh_document_chunk_fts() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            UPDATE chunk
            SET header_text = header_text
            WHERE page_id = NEW.id;
            RETURN NEW;
        END
        $$;



CREATE FUNCTION public.update_chunk_fts() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        DECLARE
            page_title text;
        BEGIN
            IF NEW.page_id IS NOT NULL THEN
                SELECT title INTO page_title FROM page WHERE id = NEW.page_id;
            ELSE
                page_title := NULL;
            END IF;

            NEW.fts :=
                setweight(
                    to_tsvector('russian', coalesce(page_title, '')) ||
                    to_tsvector('english', coalesce(page_title, '')),
                    'A'
                ) ||
                setweight(
                    to_tsvector('russian', coalesce(NEW.header_text, '')) ||
                    to_tsvector('english', coalesce(NEW.header_text, '')),
                    'A'
                ) ||
                setweight(
                    to_tsvector('russian', coalesce(NEW.section_path, '')) ||
                    to_tsvector('english', coalesce(NEW.section_path, '')) ||
                    to_tsvector('simple', coalesce(array_to_string(NEW.entity_terms, ' '), '')),
                    'B'
                ) ||
                setweight(
                    to_tsvector('russian', coalesce(NEW.text, '')) ||
                    to_tsvector('english', coalesce(NEW.text, '')),
                    'C'
                );
            RETURN NEW;
        END
        $$;





CREATE TABLE public.admin_event (
    id integer NOT NULL,
    user_id integer,
    user_email character varying(254) NOT NULL,
    ip_address character varying(64),
    event_name character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);



CREATE SEQUENCE public.admin_event_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.admin_event_id_seq OWNED BY public.admin_event.id;



CREATE TABLE public.api_client (
    id integer NOT NULL,
    name character varying(128) NOT NULL,
    client_id character varying(64) NOT NULL,
    encrypted_secret text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone
);



CREATE SEQUENCE public.api_client_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.api_client_id_seq OWNED BY public.api_client.id;



CREATE TABLE public.api_client_source (
    api_client_id integer NOT NULL,
    source_id integer NOT NULL
);



CREATE TABLE public.chat (
    id character varying(36) NOT NULL,
    title character varying(200) NOT NULL,
    user_uid character varying(256) NOT NULL,
    meta jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone
);



CREATE SEQUENCE public.chat_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.chat_id_seq OWNED BY public.chat.id;



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
    guardrail_reasons character varying[]
);



CREATE SEQUENCE public.chat_msg_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.chat_msg_id_seq OWNED BY public.chat_msg.id;



CREATE TABLE public.chunk (
    id integer NOT NULL,
    chat_id character varying(36),
    user_uid character varying(256) NOT NULL,
    msg_id integer,
    page_id integer,
    chunk_ix integer NOT NULL,
    start_offset integer,
    end_offset integer,
    text text CONSTRAINT chunk_content_not_null NOT NULL,
    fts tsvector,
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
    duplicate_of_chunk_id integer
);



CREATE SEQUENCE public.chunk_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.chunk_id_seq OWNED BY public.chunk.id;



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



CREATE SEQUENCE public.crawl_run_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.crawl_run_id_seq OWNED BY public.crawl_run.id;



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
    discover_source text
);



CREATE SEQUENCE public.document_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.document_id_seq OWNED BY public.page.id;



CREATE TABLE public.page_link (
    id bigint NOT NULL,
    source_uri text NOT NULL,
    target_uri text NOT NULL,
    source_page_id integer,
    target_page_id integer,
    source_id integer,
    found_at timestamp with time zone DEFAULT now() NOT NULL
);



CREATE SEQUENCE public.page_link_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.page_link_id_seq OWNED BY public.page_link.id;



CREATE TABLE public.page_shingle (
    page_id integer NOT NULL,
    shingle_hash bigint NOT NULL,
    source_id integer NOT NULL
);



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



CREATE SEQUENCE public.request_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.request_id_seq OWNED BY public.request.id;






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



CREATE SEQUENCE public.sitemap_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.sitemap_id_seq OWNED BY public.sitemap.id;



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



CREATE SEQUENCE public.source_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.source_id_seq OWNED BY public.source.id;



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



CREATE SEQUENCE public.trigger_response_cache_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.trigger_response_cache_id_seq OWNED BY public.trigger_response_cache.id;



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



CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;



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

Сгенерируй ровно 3 коротких следующих вопроса или действия от лица пользователя.
Подсказки должны быть напрямую связаны с последним вопросом, финальным ответом ассистента и использованными источниками.
Не повторяй уже отвеченный вопрос и не предлагай вопрос, на который финальный ответ уже дал ответ.
Каждая подсказка должна вести к новому следующему шагу: уточнить детали, сравнить варианты, открыть источник, посмотреть связанные программы или условия.
Не придумывай факты, которых нет в ответе или источниках.
Пиши на языке последнего вопроса пользователя.

Верни только JSON-объект:
{"actions": ["Короткая подсказка", "Короткая подсказка", "Короткая подсказка"]}
Для синтаксиса JSON используй только обычные двойные кавычки ASCII U+0022, не елочки «» и не типографские кавычки.

Последний вопрос пользователя:
{{user_question}}

Финальный ответ ассистента:
{{assistant_answer}}

Использованные источники:
{{sources}}
'::text NOT NULL,
    footer_text text DEFAULT '<a href="https://vbudushee.ru/faq/">Пользовательское соглашение</a>.<br>Отправить Enter, новая строка Shift+Enter'::text NOT NULL,
    welcome_messages jsonb DEFAULT '[]'::jsonb NOT NULL,
    waiting_messages jsonb DEFAULT '["Готовлю ответ"]'::jsonb NOT NULL
);



CREATE SEQUENCE public.widget_integration_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;



ALTER SEQUENCE public.widget_integration_id_seq OWNED BY public.widget_integration.id;



ALTER TABLE ONLY public.admin_event ALTER COLUMN id SET DEFAULT nextval('public.admin_event_id_seq'::regclass);



ALTER TABLE ONLY public.api_client ALTER COLUMN id SET DEFAULT nextval('public.api_client_id_seq'::regclass);



ALTER TABLE ONLY public.chat ALTER COLUMN id SET DEFAULT nextval('public.chat_id_seq'::regclass);



ALTER TABLE ONLY public.chat_msg ALTER COLUMN id SET DEFAULT nextval('public.chat_msg_id_seq'::regclass);



ALTER TABLE ONLY public.chunk ALTER COLUMN id SET DEFAULT nextval('public.chunk_id_seq'::regclass);



ALTER TABLE ONLY public.crawl_run ALTER COLUMN id SET DEFAULT nextval('public.crawl_run_id_seq'::regclass);



ALTER TABLE ONLY public.page ALTER COLUMN id SET DEFAULT nextval('public.document_id_seq'::regclass);



ALTER TABLE ONLY public.page_link ALTER COLUMN id SET DEFAULT nextval('public.page_link_id_seq'::regclass);



ALTER TABLE ONLY public.request ALTER COLUMN id SET DEFAULT nextval('public.request_id_seq'::regclass);



ALTER TABLE ONLY public.sitemap ALTER COLUMN id SET DEFAULT nextval('public.sitemap_id_seq'::regclass);



ALTER TABLE ONLY public.source ALTER COLUMN id SET DEFAULT nextval('public.source_id_seq'::regclass);



ALTER TABLE ONLY public.trigger_response_cache ALTER COLUMN id SET DEFAULT nextval('public.trigger_response_cache_id_seq'::regclass);



ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);



ALTER TABLE ONLY public.widget_integration ALTER COLUMN id SET DEFAULT nextval('public.widget_integration_id_seq'::regclass);



ALTER TABLE ONLY public.admin_event
    ADD CONSTRAINT admin_event_pkey PRIMARY KEY (id);



ALTER TABLE ONLY public.api_client
    ADD CONSTRAINT api_client_pkey PRIMARY KEY (id);



ALTER TABLE ONLY public.api_client_source
    ADD CONSTRAINT api_client_source_pkey PRIMARY KEY (api_client_id, source_id);



ALTER TABLE ONLY public.chat_msg
    ADD CONSTRAINT chat_msg_pkey PRIMARY KEY (id);



ALTER TABLE ONLY public.chat
    ADD CONSTRAINT chat_pkey PRIMARY KEY (id);



ALTER TABLE ONLY public.chunk
    ADD CONSTRAINT chunk_pkey PRIMARY KEY (id);



ALTER TABLE ONLY public.crawl_run
    ADD CONSTRAINT crawl_run_pkey PRIMARY KEY (id);



ALTER TABLE ONLY public.page
    ADD CONSTRAINT document_pkey PRIMARY KEY (id);



ALTER TABLE ONLY public.page_link
    ADD CONSTRAINT page_link_pkey PRIMARY KEY (id);



ALTER TABLE ONLY public.page_shingle
    ADD CONSTRAINT page_shingle_pkey PRIMARY KEY (page_id, shingle_hash);



ALTER TABLE ONLY public.request
    ADD CONSTRAINT request_pkey PRIMARY KEY (id);






ALTER TABLE ONLY public.sitemap
    ADD CONSTRAINT sitemap_pkey PRIMARY KEY (id);



ALTER TABLE ONLY public.source
    ADD CONSTRAINT source_pkey PRIMARY KEY (id);



ALTER TABLE ONLY public.trigger_response_cache
    ADD CONSTRAINT trigger_response_cache_pkey PRIMARY KEY (id);



ALTER TABLE ONLY public.trigger_response_cache
    ADD CONSTRAINT uq_trigger_response_cache_page_trigger_prompt UNIQUE (page_id, trigger_key, prompt_hash);



ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);



ALTER TABLE ONLY public.widget_integration
    ADD CONSTRAINT widget_integration_pkey PRIMARY KEY (id);



CREATE INDEX ix_admin_event_event_name ON public.admin_event USING btree (event_name);



CREATE INDEX ix_admin_event_user_id ON public.admin_event USING btree (user_id);



CREATE UNIQUE INDEX ix_api_client_client_id ON public.api_client USING btree (client_id);



CREATE INDEX ix_api_client_source_source_id ON public.api_client_source USING btree (source_id);



CREATE INDEX ix_chat_msg_chat_id ON public.chat_msg USING btree (chat_id);



CREATE INDEX ix_chat_msg_guardrail_triggered ON public.chat_msg USING btree (guardrail_triggered);



CREATE INDEX ix_chat_msg_text_fts_simple ON public.chat_msg USING gin (to_tsvector('simple'::regconfig, COALESCE(text, ''::text))) WHERE (role = ANY (ARRAY['user'::public.chatrole, 'assistant'::public.chatrole]));



CREATE INDEX ix_chat_msg_user_uid ON public.chat_msg USING btree (user_uid);



CREATE INDEX ix_chat_search_fts_simple ON public.chat USING gin (to_tsvector('simple'::regconfig, (((COALESCE(title, ''::character varying))::text || ' '::text) || (COALESCE(user_uid, ''::character varying))::text)));



CREATE INDEX ix_chat_user_uid ON public.chat USING btree (user_uid);



CREATE INDEX ix_chunk_chat_id ON public.chunk USING btree (chat_id);



CREATE INDEX ix_chunk_chat_kind ON public.chunk USING btree (chat_id, kind);



CREATE INDEX ix_chunk_document_id ON public.chunk USING btree (page_id);



CREATE INDEX ix_chunk_document_kind ON public.chunk USING btree (page_id, kind);



CREATE INDEX ix_chunk_duplicate_of_chunk_id ON public.chunk USING btree (duplicate_of_chunk_id);



CREATE INDEX ix_chunk_embedding_chat_hnsw_cosine ON public.chunk USING hnsw (embedding public.vector_cosine_ops) WHERE ((chat_id IS NOT NULL) AND (is_duplicate = false) AND (embedding IS NOT NULL));



CREATE INDEX ix_chunk_embedding_kb_hnsw_cosine ON public.chunk USING hnsw (embedding public.vector_cosine_ops) WHERE ((chat_id IS NULL) AND (page_id IS NOT NULL) AND (is_duplicate = false) AND (embedding IS NOT NULL));



CREATE INDEX ix_chunk_fts ON public.chunk USING gin (fts);



CREATE INDEX ix_chunk_is_duplicate ON public.chunk USING btree (is_duplicate);



CREATE INDEX ix_chunk_kind ON public.chunk USING btree (kind);



CREATE INDEX ix_chunk_msg_id ON public.chunk USING btree (msg_id);



CREATE INDEX ix_chunk_text_hash ON public.chunk USING btree (text_hash);



CREATE INDEX ix_chunk_user_uid ON public.chunk USING btree (user_uid);



CREATE INDEX ix_document_source_id ON public.page USING btree (source_id);



CREATE INDEX ix_page_discover_by ON public.page USING btree (discover_by);



CREATE INDEX ix_page_discovered_via ON public.page USING btree (discovered_via);



CREATE INDEX ix_page_has_triggers ON public.page USING btree (has_triggers);



CREATE INDEX ix_page_link_source_page_id ON public.page_link USING btree (source_page_id);



CREATE INDEX ix_page_link_target_page_id ON public.page_link USING btree (target_page_id);



CREATE INDEX ix_page_shingle_source_hash ON public.page_shingle USING btree (source_id, shingle_hash);



CREATE INDEX ix_page_shingle_source_id ON public.page_shingle USING btree (source_id);



CREATE INDEX ix_page_status ON public.page USING btree (status);



CREATE INDEX ix_page_status_error ON public.page USING btree (status_error);



CREATE INDEX ix_source_is_paused ON public.source USING btree (is_paused);



CREATE INDEX ix_trigger_response_cache_page_id ON public.trigger_response_cache USING btree (page_id);



CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);



CREATE UNIQUE INDEX ix_widget_integration_code ON public.widget_integration USING btree (code);



CREATE UNIQUE INDEX uq_page_uri ON public.page USING btree (uri);



CREATE TRIGGER trigger_promote_duplicate_chunk_on_delete BEFORE DELETE ON public.chunk FOR EACH ROW EXECUTE FUNCTION public.promote_duplicate_chunk_on_delete();



CREATE TRIGGER trigger_refresh_document_chunk_fts AFTER UPDATE OF title ON public.page FOR EACH ROW EXECUTE FUNCTION public.refresh_document_chunk_fts();



CREATE TRIGGER trigger_update_chunk_fts BEFORE INSERT OR UPDATE OF text, header_text, section_path, entity_terms, page_id ON public.chunk FOR EACH ROW EXECUTE FUNCTION public.update_chunk_fts();



ALTER TABLE ONLY public.admin_event
    ADD CONSTRAINT admin_event_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;



ALTER TABLE ONLY public.api_client_source
    ADD CONSTRAINT api_client_source_api_client_id_fkey FOREIGN KEY (api_client_id) REFERENCES public.api_client(id) ON DELETE CASCADE;



ALTER TABLE ONLY public.api_client_source
    ADD CONSTRAINT api_client_source_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id) ON DELETE CASCADE;



ALTER TABLE ONLY public.chunk
    ADD CONSTRAINT chunk_msg_id_fkey FOREIGN KEY (msg_id) REFERENCES public.chat_msg(id);



ALTER TABLE ONLY public.chunk
    ADD CONSTRAINT chunk_page_id_fkey FOREIGN KEY (page_id) REFERENCES public.page(id) ON DELETE CASCADE;



ALTER TABLE ONLY public.crawl_run
    ADD CONSTRAINT crawl_run_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id) ON DELETE CASCADE;



ALTER TABLE ONLY public.page
    ADD CONSTRAINT document_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id) ON DELETE CASCADE;



ALTER TABLE ONLY public.chat_msg
    ADD CONSTRAINT fk_chat_msg_chat_id_chat FOREIGN KEY (chat_id) REFERENCES public.chat(id) ON DELETE CASCADE;



ALTER TABLE ONLY public.chunk
    ADD CONSTRAINT fk_chunk_chat_id_chat FOREIGN KEY (chat_id) REFERENCES public.chat(id) ON DELETE CASCADE;



ALTER TABLE ONLY public.request
    ADD CONSTRAINT fk_request_chat_id_chat FOREIGN KEY (chat_id) REFERENCES public.chat(id);



ALTER TABLE ONLY public.page_link
    ADD CONSTRAINT page_link_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id) ON DELETE CASCADE;



ALTER TABLE ONLY public.page_link
    ADD CONSTRAINT page_link_source_page_id_fkey FOREIGN KEY (source_page_id) REFERENCES public.page(id) ON DELETE CASCADE;



ALTER TABLE ONLY public.page_link
    ADD CONSTRAINT page_link_target_page_id_fkey FOREIGN KEY (target_page_id) REFERENCES public.page(id) ON DELETE CASCADE;



ALTER TABLE ONLY public.page_shingle
    ADD CONSTRAINT page_shingle_page_id_fkey FOREIGN KEY (page_id) REFERENCES public.page(id) ON DELETE CASCADE;



ALTER TABLE ONLY public.page_shingle
    ADD CONSTRAINT page_shingle_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id) ON DELETE CASCADE;



ALTER TABLE ONLY public.sitemap
    ADD CONSTRAINT sitemap_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.source(id) ON DELETE CASCADE;



ALTER TABLE ONLY public.trigger_response_cache
    ADD CONSTRAINT trigger_response_cache_page_id_fkey FOREIGN KEY (page_id) REFERENCES public.page(id) ON DELETE CASCADE;
