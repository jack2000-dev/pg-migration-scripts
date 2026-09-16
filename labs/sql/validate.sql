-- Run on source and target after writes are frozen and catch-up is complete.
-- Identical databases must return an identical JSON value.
WITH summary AS (
    SELECT
        (SELECT count(*) FROM public.accounts) AS account_count,
        (SELECT min(account_id) FROM public.accounts) AS min_account_id,
        (SELECT max(account_id) FROM public.accounts) AS max_account_id,
        (SELECT COALESCE(sum(balance), 0) FROM public.accounts) AS balance_total,
        (SELECT count(*) FROM public.transfers) AS transfer_count,
        (SELECT min(transfer_id) FROM public.transfers) AS min_transfer_id,
        (SELECT max(transfer_id) FROM public.transfers) AS max_transfer_id,
        (SELECT COALESCE(sum(amount), 0) FROM public.transfers) AS amount_total,
        (SELECT count(*) FROM public.event_log) AS event_count,
        (SELECT count(*) FROM public.transfers t
         LEFT JOIN public.accounts a1 ON a1.account_id = t.from_account_id
         LEFT JOIN public.accounts a2 ON a2.account_id = t.to_account_id
         WHERE a1.account_id IS NULL OR a2.account_id IS NULL) AS orphan_transfers,
        (SELECT count(*) FROM public.event_log e
         LEFT JOIN public.transfers t USING (transfer_id)
         WHERE t.transfer_id IS NULL) AS orphan_events,
        (SELECT count(*) FROM public.transfers t
         LEFT JOIN public.event_log e USING (transfer_id)
         WHERE e.transfer_id IS NULL) AS missing_events
)
SELECT jsonb_build_object(
    'database', current_database(),
    'account_count', account_count,
    'account_id_range', jsonb_build_array(min_account_id, max_account_id),
    'balance_total', balance_total,
    'transfer_count', transfer_count,
    'transfer_id_range', jsonb_build_array(min_transfer_id, max_transfer_id),
    'amount_total', amount_total,
    'event_count', event_count,
    'orphan_transfers', orphan_transfers,
    'orphan_events', orphan_events,
    'missing_events', missing_events,
    'valid', account_count > 1
             AND balance_total = account_count * 100000
             AND event_count = transfer_count
             AND orphan_transfers = 0
             AND orphan_events = 0
             AND missing_events = 0
)::text
FROM summary;
