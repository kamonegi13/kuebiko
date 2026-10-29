-- replay 専用の read-only ロール (調整役が別途実行する。replay のコードは実行しない)。
-- 付与は CONNECT + USAGE + SELECT のみ。作成・書き込み・関数の EXECUTE の追加はしない。
-- パスワードは実行時に psql の変数で渡し、ファイルにもコミットにも書かない:
--   psql -v pw="'...'" -f scripts/replay/create_readonly_role.sql
-- 接続: REPLAY_DATABASE_URL=postgresql://kuebiko_replay_ro:<pw>@127.0.0.1:5433/kuebiko

CREATE ROLE kuebiko_replay_ro LOGIN PASSWORD :pw
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

ALTER ROLE kuebiko_replay_ro SET default_transaction_read_only = on;
ALTER ROLE kuebiko_replay_ro SET statement_timeout = '10min';

REVOKE ALL ON DATABASE kuebiko FROM kuebiko_replay_ro;
GRANT CONNECT ON DATABASE kuebiko TO kuebiko_replay_ro;

REVOKE ALL ON SCHEMA public FROM kuebiko_replay_ro;
GRANT USAGE ON SCHEMA public TO kuebiko_replay_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO kuebiko_replay_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO kuebiko_replay_ro;

-- PUBLIC に付いている関数の EXECUTE (set_config / pg_sleep 等) は残る。
-- 防御の一層目は「書けない」こと (default_transaction_read_only + SELECT のみ)。
-- 資格情報の入った表 (app_config_versions の接続設定等) が public にあるなら、
-- replay が使う表だけに GRANT を絞ってもよい (その場合は上の GRANT ALL TABLES を置き換える)。
