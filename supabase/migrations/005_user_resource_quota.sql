-- Existing 2 CPU / 4 GiB Boxes retain their allocation and consume the full quota.
DROP INDEX web_terminal.one_box_per_account;
ALTER TABLE web_terminal.boxes DROP CONSTRAINT boxes_cpus_check,
    DROP CONSTRAINT boxes_memory_mib_check, DROP CONSTRAINT boxes_disk_size_gb_check;
ALTER TABLE web_terminal.boxes ADD CONSTRAINT boxes_cpus_check CHECK(cpus BETWEEN 1 AND 2),
    ADD CONSTRAINT boxes_memory_mib_check CHECK(memory_mib BETWEEN 512 AND 4096),
    ADD CONSTRAINT boxes_disk_size_gb_check CHECK(disk_size_gb BETWEEN 1 AND 10);
CREATE INDEX boxes_owner_current ON web_terminal.boxes(owner_id) WHERE deleted_at IS NULL;

CREATE FUNCTION web_terminal.enforce_resource_quota()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE used_cpus bigint; used_memory bigint;
BEGIN
    IF TG_OP='UPDATE' AND NEW.owner_id<>OLD.owner_id THEN
        RAISE EXCEPTION 'Box ownership is immutable' USING ERRCODE='23514';
    END IF;
    IF NEW.deleted_at IS NOT NULL THEN RETURN NEW; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.owner_id::text,793105733));
    SELECT coalesce(sum(cpus),0),coalesce(sum(memory_mib),0) INTO used_cpus,used_memory
      FROM web_terminal.boxes WHERE owner_id=NEW.owner_id AND deleted_at IS NULL AND id<>NEW.id;
    IF used_cpus+NEW.cpus>2 OR used_memory+NEW.memory_mib>4096 THEN
        RAISE EXCEPTION 'Account quota exceeded: total limit is 2 CPU / 4 GiB; delete a Box to release its allocation'
            USING ERRCODE='23514',CONSTRAINT='user_resource_quota';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION web_terminal.enforce_resource_quota() FROM PUBLIC,anon,authenticated;
CREATE TRIGGER enforce_resource_quota BEFORE INSERT OR UPDATE OF owner_id,cpus,memory_mib,deleted_at
    ON web_terminal.boxes FOR EACH ROW EXECUTE FUNCTION web_terminal.enforce_resource_quota();
