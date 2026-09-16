\set account_a random(1, :account_count)
\set account_offset random(1, :account_count - 1)
\set account_b ((:account_a + :account_offset - 1) % :account_count) + 1
\set transfer_amount random(1, 100)

BEGIN;
WITH debit AS (
    UPDATE public.accounts
    SET balance = balance - :transfer_amount,
        updated_at = clock_timestamp()
    WHERE account_id = :account_a AND balance >= :transfer_amount
    RETURNING account_id
), credit AS (
    UPDATE public.accounts
    SET balance = balance + :transfer_amount,
        updated_at = clock_timestamp()
    WHERE account_id = :account_b AND EXISTS (SELECT FROM debit)
    RETURNING account_id
), new_transfer AS (
    INSERT INTO public.transfers
        (from_account_id, to_account_id, amount)
    SELECT :account_a, :account_b, :transfer_amount
    FROM debit, credit
    RETURNING transfer_id
)
INSERT INTO public.event_log (transfer_id, event_type)
SELECT transfer_id, 'transfer.created' FROM new_transfer;
COMMIT;
