-- pg_dump -d core_prod --file=schema.sql --schema-only --schema=public --no-owner --no-acl
--
-- PostgreSQL database dump
--
-- Dumped from database version 17.0 (Homebrew)
-- Dumped by pg_dump version 17.0 (Homebrew)
SET
    statement_timeout = 0;

SET
    lock_timeout = 0;

SET
    idle_in_transaction_session_timeout = 0;

SET
    transaction_timeout = 0;

SET
    client_encoding = 'UTF8';

SET
    standard_conforming_strings = on;

SELECT
    pg_catalog.set_config('search_path', '', false);

SET
    check_function_bodies = false;

SET
    xmloption = content;

SET
    client_min_messages = warning;

SET
    row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--
CREATE SCHEMA IF NOT EXISTS public;

--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--
COMMENT ON SCHEMA public IS 'standard public schema';

CREATE TYPE public.userrole AS ENUM (
    'admin',
    'guest',
    'user'
);

-- --
-- -- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
-- --
-- CREATE TABLE public.alembic_version (version_num character varying(32) NOT NULL);
--
-- Name: post; Type: TABLE; Schema: public; Owner: -
--
CREATE TABLE public.post (
    id integer NOT NULL,
    short_id character varying(20) NOT NULL,
    title character varying(250) NOT NULL,
    slug character varying(250) NOT NULL,
    lead text,
    body text,
    body_html text,
    body_toc text,
    user_id integer NOT NULL,
    picture character varying(500),
    show_toc boolean DEFAULT false NOT NULL,
    is_published boolean DEFAULT false NOT NULL,
    published_at timestamp with time zone,
    search tsvector GENERATED ALWAYS AS (
        (
            setweight(
                to_tsvector(
                    'simple' :: regconfig,
                    (COALESCE(title, '' :: character varying)) :: text
                ),
                'A' :: "char"
            ) || setweight(
                to_tsvector(
                    'simple' :: regconfig,
                    COALESCE(body, '' :: text)
                ),
                'B' :: "char"
            )
        )
    ) STORED,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone
);

--
-- Name: post_category; Type: TABLE; Schema: public; Owner: -
--
CREATE TABLE public.post_category (
    id integer NOT NULL,
    slug character varying(100) NOT NULL,
    title character varying(100) NOT NULL,
    description text NOT NULL,
    description_html text NOT NULL,
    is_tag boolean NOT NULL,
    is_term boolean NOT NULL
);

--
-- Name: post_category_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--
CREATE SEQUENCE public.post_category_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;

--
-- Name: post_category_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--
ALTER SEQUENCE public.post_category_id_seq OWNED BY public.post_category.id;

--
-- Name: post_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--
CREATE SEQUENCE public.post_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;

--
-- Name: post_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--
ALTER SEQUENCE public.post_id_seq OWNED BY public.post.id;

--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--
CREATE TABLE public.users (
    id integer NOT NULL,
    email character varying NOT NULL,
    password character varying NOT NULL,
    role public.userrole DEFAULT 'guest' :: public.userrole NOT NULL,
    name character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    language character varying(2) DEFAULT 'en' :: character varying NOT NULL
);

--
-- Name: user_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--
CREATE SEQUENCE public.user_id_seq AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;

--
-- Name: user_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--
ALTER SEQUENCE public.user_id_seq OWNED BY public.users.id;

--
-- Name: post id; Type: DEFAULT; Schema: public; Owner: -
--
ALTER TABLE
    ONLY public.post
ALTER COLUMN
    id
SET
    DEFAULT nextval('public.post_id_seq' :: regclass);

--
-- Name: post_category id; Type: DEFAULT; Schema: public; Owner: -
--
ALTER TABLE
    ONLY public.post_category
ALTER COLUMN
    id
SET
    DEFAULT nextval('public.post_category_id_seq' :: regclass);

--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--
ALTER TABLE
    ONLY public.users
ALTER COLUMN
    id
SET
    DEFAULT nextval('public.user_id_seq' :: regclass);

--
-- Name: post_category post_category_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE
    ONLY public.post_category
ADD
    CONSTRAINT post_category_pkey PRIMARY KEY (id);

--
-- Name: post post_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE
    ONLY public.post
ADD
    CONSTRAINT post_pkey PRIMARY KEY (id);

--
-- Name: post post_short_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE
    ONLY public.post
ADD
    CONSTRAINT post_short_id_key UNIQUE (short_id);

--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE
    ONLY public.users
ADD
    CONSTRAINT users_pkey PRIMARY KEY (id);

--
-- Name: ix_post_is_published; Type: INDEX; Schema: public; Owner: -
--
CREATE INDEX ix_post_is_published ON public.post USING btree (is_published);

--
-- Name: ix_post_published_at; Type: INDEX; Schema: public; Owner: -
--
CREATE INDEX ix_post_published_at ON public.post USING btree (published_at);

--
-- Name: ix_post_user_id; Type: INDEX; Schema: public; Owner: -
--
CREATE INDEX ix_post_user_id ON public.post USING btree (user_id);

--
-- Name: ix_users_email; Type: INDEX; Schema: public; Owner: -
--
CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);

--
-- Name: post post_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE
    ONLY public.post
ADD
    CONSTRAINT post_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);
