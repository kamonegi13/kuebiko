-- replay 専用の read-only ロール (調整役が別途実行する。replay のコードは実行しない)。
-- 付与は CONNECT + USAGE + SELECT のみ。冪等 (何度流しても同じ状態になる)。
-- 実行 (所有者 kuebiko で。パスワードは変数で渡し、ファイルにもコミットにも書かない):
--   psql -v pw='...' -d kuebiko -f scripts/replay/create_readonly_role.sql
-- 接続: REPLAY_DATABASE_URL=postgresql://kuebiko_replay_ro:<pw>@127.0.0.1:5433/kuebiko

-- ロールが無いときだけ作る (CREATE ROLE は IF NOT EXISTS が無いので \gexec で分岐)。
SELECT 'CREATE ROLE kuebiko_replay_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
       'NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kuebiko_replay_ro')
\gexec

-- パスワードは毎回設定し直す (:'pw' は psql が文字列リテラルとして安全に引用する)。
SELECT format('ALTER ROLE kuebiko_replay_ro PASSWORD %L', :'pw')
\gexec

ALTER ROLE kuebiko_replay_ro SET default_transaction_read_only = on;
ALTER ROLE kuebiko_replay_ro SET statement_timeout = '60s';

REVOKE ALL ON DATABASE kuebiko FROM kuebiko_replay_ro;
GRANT CONNECT ON DATABASE kuebiko TO kuebiko_replay_ro;

REVOKE ALL ON SCHEMA public FROM kuebiko_replay_ro;
GRANT USAGE ON SCHEMA public TO kuebiko_replay_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO kuebiko_replay_ro;
-- 今後 kuebiko (表の所有者) が作る表にも SELECT だけ付ける。
ALTER DEFAULT PRIVILEGES FOR ROLE kuebiko IN SCHEMA public
    GRANT SELECT ON TABLES TO kuebiko_replay_ro;

-- 残る穴 (正直に):
--  * TEMP: データベースの TEMP 権限は PUBLIC に付いており、ロール単位では外せない
--    (REVOKE ... FROM kuebiko_replay_ro は PUBLIC 経由の継承を消さない)。
--    `REVOKE TEMP ON DATABASE kuebiko FROM PUBLIC` は他のロール (アプリ本体) の一時表を
--    壊しうるため行わない。read-only のトランザクションでは一時表を作れないので、
--    default_transaction_read_only が守る間は実害が無い。
--  * PUBLIC に付いた関数の EXECUTE (set_config / pg_sleep 等) も残る。ロール自身が
--    SET default_transaction_read_only = off できるため、最後の砦は
--    「SELECT しか付与していない」ことと、クライアント側の SQL 検査・statement_timeout。
--  * 資格情報を含む表 (app_config_versions の接続設定等) が public にあるなら、
--    GRANT SELECT ON ALL TABLES を replay が読む表だけの GRANT に置き換えてよい。
