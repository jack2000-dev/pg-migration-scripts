-- Run only the block matching the pgAdmin connection/database shown in its
-- heading. Replace the connection placeholders without committing edits.
-- The password is stored in the protected pg_subscription catalog.

-- SOURCE / lab_db1
SET ROLE lab_owner;
CREATE PUBLICATION lab_forward_pub_db1 FOR TABLE
    public.accounts, public.transfers, public.event_log,
    cutover_control.replication_probe
WITH (publish = 'insert, update, delete, truncate');
RESET ROLE;

-- TARGET / lab_db1
CREATE SUBSCRIPTION lab_forward_sub_db1
CONNECTION 'host=SOURCE_HOST port=25060 dbname=lab_db1 user=lab_forward_repl password=SOURCE_REPLICATION_PASSWORD sslmode=verify-full sslrootcert=/absolute/path/to/ca.crt'
PUBLICATION lab_forward_pub_db1
WITH (copy_data=true, create_slot=true, slot_name='lab_forward_slot_db1',
      enabled=true, binary=false, streaming=on, disable_on_error=true);

-- SOURCE / lab_db2
SET ROLE lab_owner;
CREATE PUBLICATION lab_forward_pub_db2 FOR TABLE
    public.accounts, public.transfers, public.event_log,
    cutover_control.replication_probe
WITH (publish = 'insert, update, delete, truncate');
RESET ROLE;

-- TARGET / lab_db2
CREATE SUBSCRIPTION lab_forward_sub_db2
CONNECTION 'host=SOURCE_HOST port=25060 dbname=lab_db2 user=lab_forward_repl password=SOURCE_REPLICATION_PASSWORD sslmode=verify-full sslrootcert=/absolute/path/to/ca.crt'
PUBLICATION lab_forward_pub_db2
WITH (copy_data=true, create_slot=true, slot_name='lab_forward_slot_db2',
      enabled=true, binary=false, streaming=on, disable_on_error=true);
