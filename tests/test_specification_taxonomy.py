from __future__ import annotations

import unittest

from app.analyzer.specification_taxonomy import SpecificationTaxonomyLoader


class SpecificationTaxonomyTests(unittest.TestCase):
    def test_taxonomy_loads_and_has_catalog_rules(self) -> None:
        taxonomy = SpecificationTaxonomyLoader.load(force_reload=True)
        self.assertTrue(taxonomy.version)
        self.assertGreaterEqual(len(taxonomy.spec_types), 20)
        self.assertGreaterEqual(len(taxonomy.exact_rules), 150)

    def test_subtype_codes_are_globally_unique(self) -> None:
        taxonomy = SpecificationTaxonomyLoader.load()
        codes = [
            subtype.code
            for spec_type in taxonomy.spec_types
            for subtype in spec_type.subtypes
        ]
        self.assertEqual(len(codes), len(set(codes)))

    def test_catalog_contains_region_specific_same_number_rules(self) -> None:
        taxonomy = SpecificationTaxonomyLoader.load()
        rules_555 = [rule for rule in taxonomy.exact_rules if rule.spec_id == "555"]
        identities = {(rule.region_scope, rule.spec_type) for rule in rules_555}
        self.assertIn(("NA", "VIRTUAL_ASSISTANT"), identities)
        self.assertIn(("EXCEPT_NA", "VUI"), identities)


if __name__ == "__main__":
    unittest.main()
