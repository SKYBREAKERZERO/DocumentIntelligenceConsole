# Two-Spec SQL Reference Extraction Test Plan

Use project/scope **24MM** for both documents.

## Files

1. `1001_User_Profile_Main_Function_Spec_Rev1.0.docx`
2. `2007_User_Profile_Communication_Interface_Spec_Rev2.1.docx`

## Recommended import order

### Step 1 - import 1001 first

At this point 2007 is not yet in the PostgreSQL specification registry.

Expected current reference facts for 1001:

| Source | Target | Expected status | Why |
|---|---|---|---|
| 1001 | `SPEC:2007` | `UNRESOLVED` | Explicit reference exists but target Master is not imported yet |
| 1001 | `SPEC:3999` | `UNRESOLVED` | Deliberately missing test specification |

The following must **not** create specification edges:

- `2007 ms` numeric timeout example
- `本书第5.1节` internal section reference
- `ISO 8601`
- `JIS Z 8301`

### Step 2 - import 2007

When 2007 is saved, `reconcile_unresolved()` runs against the PostgreSQL specification registry.

Expected results:

| Source | Target | Expected status | Expected resolution |
|---|---|---|---|
| 1001 | 2007 | `CONFIRMED` | exact SQL registry reconciliation |
| 1001 | 3999 | `UNRESOLVED` | target still absent |
| 2007 | 1001 | `CONFIRMED` | exact code/title match |
| 2007 | `车辆隐私处理共通式样书` | `UNRESOLVED` | quoted title not present in registry |

The following must **not** create specification edges:

- `1001 bytes` numeric payload example
- `See Section 4.3` internal section reference
- `ISO 8601`

## Audit

After running the v6.2 SQL helper upgrade:

```sql
SELECT
    source_specification_key,
    target_specification_key,
    target_external_key,
    extraction_method,
    confidence,
    status,
    evidence_count
FROM public.spec_reference_current_edges
ORDER BY source_specification_id, relation_id;
```

Expected logical graph:

```text
1001 --REFERENCES--> 2007      CONFIRMED
1001 --REFERENCES--> 3999      UNRESOLVED
2007 --REFERENCES--> 1001      CONFIRMED
2007 --REFERENCES--> 车辆隐私处理共通式样书   UNRESOLVED
```

## Chunk/Evidence verification

Open each reference in Engineering Relations and inspect Evidence.

Every stored evidence row must have an exact `content_id` mapping to `contents.id`.

Examples you should see:

- 1001: `Specification ２００７ 第4.2节`
- 1001: `User Profile Communication Interface Spec`
- 1001: `Specification 3999`
- 2007: `User Profile Main Function Spec`
- 2007: `Specification 1001`
- 2007: `《车辆隐私处理共通式样书》`

No vector/Qdrant lookup is needed to produce these relations.
