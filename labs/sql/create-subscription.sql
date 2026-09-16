\set ON_ERROR_STOP on
\if :{?subscription}
\else
\echo 'ERROR: pass -v subscription=...'
\quit 3
\endif
\if :{?publication}
\else
\echo 'ERROR: pass -v publication=...'
\quit 3
\endif
\if :{?slot}
\else
\echo 'ERROR: pass -v slot=...'
\quit 3
\endif
\getenv source_conninfo FORWARD_SOURCE_CONNINFO
\if :{?source_conninfo}
\else
\echo 'ERROR: FORWARD_SOURCE_CONNINFO is not set'
\quit 3
\endif

CREATE SUBSCRIPTION :"subscription"
CONNECTION :'source_conninfo'
PUBLICATION :"publication"
WITH (copy_data=true, create_slot=true, slot_name=:'slot',
      enabled=true, binary=false, streaming=on, disable_on_error=true);
