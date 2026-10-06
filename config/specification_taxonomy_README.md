# Specification Taxonomy v1

`specification_taxonomy.yaml` is the classification dictionary used by
`SpecificationClassifier`. Classification rules are data, not Python code.

## Authority boundaries

- `project_code` / processing scope (`21MM` / `24MM` / `Common`) is selected
  by the GUI user and is never inferred from document text.
- `Common` is generic/non-spec mode.  The taxonomy is not applied and
  `series`, `region_scope`, `spec_type`, and `spec_subtype` remain empty.
- `region_scope`, `spec_type`, and `spec_subtype` are derived from the taxonomy.
- Document body classification is disabled by default because referenced specs
  must not redefine the current document identity.

## Matching priority

1. Manual metadata override.
2. Exact catalog rule: region + leading specification number + official-name keyword.
3. Compound subtype keyword, e.g. `Bluetooth Audio` or `Navigation HMI`.
4. Broad type keyword, e.g. `Bluetooth`, `Wi-Fi`, `HMI`, `User Profile`.
5. If no safe match exists, leave `spec_type` / `spec_subtype` as `NULL`.

## Region handling

The first vocabulary includes `NA`, `EXCEPT_NA`, and common country/combined
scopes such as `JP`, `CN`, `KR`, `TW`, `EU`, `JP_NA_CN`, etc.

The same specification number may have different meanings by region, so exact
rules are region-qualified. For example, the taxonomy can distinguish an NA
catalog entry from an Except-NA entry even when their leading numbers are the
same.

## Editing the dictionary

Add or edit entries under:

```yaml
spec_types:
  WIFI:
    display_name: Wi-Fi
    keywords:
      - Wi-Fi
      - WiFi
```

Subtypes must currently have globally unique codes because the PostgreSQL
`spec_subtypes.code` column is the primary key.

Exact catalog rules use this shape:

```yaml
exact_rules:
  - rule_id: EXCEPT_NA:550:1
    region_scope: EXCEPT_NA
    spec_id: '550'
    spec_type: BT
    spec_subtype: BLUETOOTH_AUDIO
    name_keywords:
      - Bluetooth Audio Function
```

## External dictionary in packaged builds

The loader checks an external file first:

```text
<EXE directory>/config/specification_taxonomy.yaml
```

If it does not exist, it falls back to the taxonomy bundled in the executable.
You can also set `SPECIFICATION_TAXONOMY_FILE` to an explicit file path.

## Review list

`specification_taxonomy_unresolved_v1.txt` contains first-pass catalog entries
that should be reviewed before adding an exact rule. The classifier deliberately
prefers `UNRESOLVED` over guessing.
