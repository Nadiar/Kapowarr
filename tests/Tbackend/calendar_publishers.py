# -*- coding: utf-8 -*-

"""Tests for the calendar publisher imprint mapping and categories."""

import unittest

from backend.features.calendar_publishers import (IMPRINT_MAP, NAME_ALIASES,
                                                  PRESET_PUBLISHER_NAMES,
                                                  PUBLISHER_CATEGORIES,
                                                  get_publisher_categories,
                                                  normalize_publisher_name,
                                                  resolve_parent_publisher)


class TestImprintMap(unittest.TestCase):
    """Test the name-based imprint-to-parent mapping."""

    def test_known_image_imprint_resolves(self):
        """Todd McFarlane Productions should resolve to Image."""
        result = resolve_parent_publisher(
            'Todd McFarlane Productions'
        )
        self.assertEqual(result, 'Image')

    def test_known_dc_imprint_resolves(self):
        """Vertigo should resolve to DC Comics."""
        self.assertEqual(
            resolve_parent_publisher('Vertigo'), 'DC Comics'
        )

    def test_known_marvel_imprint_resolves(self):
        """MAX should resolve to Marvel."""
        self.assertEqual(
            resolve_parent_publisher('MAX'), 'Marvel'
        )

    def test_preset_publisher_returns_none(self):
        """A preset publisher (Image) is not an imprint."""
        self.assertIsNone(resolve_parent_publisher('Image'))

    def test_unknown_publisher_returns_none_and_caches(self):
        """An unknown publisher returns None and gets cached."""
        self.assertIsNone(
            resolve_parent_publisher('Unknown Press')
        )
        # Second call should hit the cache
        self.assertIsNone(
            resolve_parent_publisher('Unknown Press')
        )

    def test_all_imprint_values_are_preset_names(self):
        """Every value in IMPRINT_MAP should be a preset name."""
        for imprint, parent in IMPRINT_MAP.items():
            self.assertIn(
                parent, PRESET_PUBLISHER_NAMES,
                f'Imprint {imprint!r} maps to {parent!r}, '
                f'which is not a preset publisher'
            )

    def test_no_imprint_is_also_a_preset(self):
        """No imprint key should also be a preset publisher name."""
        for imprint in IMPRINT_MAP:
            self.assertNotIn(
                imprint, PRESET_PUBLISHER_NAMES,
                f'{imprint!r} is both an imprint and a preset'
            )

    def test_none_input_returns_none(self):
        """None publisher name should return None."""
        self.assertIsNone(resolve_parent_publisher(None))


class TestPublisherCategories(unittest.TestCase):
    """Test publisher category groupings."""

    def test_categories_exist(self):
        """At least western and manga categories should exist."""
        self.assertIn('western', PUBLISHER_CATEGORIES)
        self.assertIn('manga', PUBLISHER_CATEGORIES)

    def test_all_presets_are_categorized(self):
        """Every preset publisher should appear in at least one category."""
        all_categorized = set()
        for cat_names in PUBLISHER_CATEGORIES.values():
            all_categorized.update(cat_names)
        self.assertEqual(
            PRESET_PUBLISHER_NAMES, all_categorized,
            f'Uncategorized: {PRESET_PUBLISHER_NAMES - all_categorized}, '
            f'Extra: {all_categorized - PRESET_PUBLISHER_NAMES}'
        )

    def test_no_duplicate_across_categories(self):
        """No publisher should appear in multiple categories."""
        seen = set()
        for cat_name, cat_names in PUBLISHER_CATEGORIES.items():
            for name in cat_names:
                self.assertNotIn(
                    name, seen,
                    f'{name!r} appears in multiple categories'
                )
                seen.add(name)

    def test_get_publisher_categories_returns_name_lists(self):
        """get_publisher_categories() returns {category: [str]}."""
        result = get_publisher_categories()
        self.assertIn('western', result)
        self.assertIn('manga', result)
        for cat_name, pubs in result.items():
            self.assertIsInstance(pubs, list)
            for pub in pubs:
                self.assertIsInstance(pub, str)

    def test_manga_contains_viz_and_kodansha(self):
        """Manga category should include VIZ Media and Kodansha."""
        self.assertIn('VIZ Media', PUBLISHER_CATEGORIES['manga'])
        self.assertIn('Kodansha Comics', PUBLISHER_CATEGORIES['manga'])


class TestNameAliases(unittest.TestCase):
    """Test publisher name normalization."""

    def test_alias_resolves_to_preset(self):
        """Every alias value must be a preset publisher name."""
        for alias, canonical in NAME_ALIASES.items():
            self.assertIn(
                canonical, PRESET_PUBLISHER_NAMES,
                f'Alias {alias!r} maps to {canonical!r}, '
                f'which is not a preset'
            )

    def test_boom_case_variant(self):
        """'Boom! Studios' normalizes to 'BOOM! Studios'."""
        self.assertEqual(
            normalize_publisher_name('Boom! Studios'),
            'BOOM! Studios'
        )

    def test_viz_short_name(self):
        """'Viz' normalizes to 'VIZ Media'."""
        self.assertEqual(
            normalize_publisher_name('Viz'),
            'VIZ Media'
        )

    def test_kodansha_without_comics(self):
        """'Kodansha' normalizes to 'Kodansha Comics'."""
        self.assertEqual(
            normalize_publisher_name('Kodansha'),
            'Kodansha Comics'
        )

    def test_kodansha_usa_variant(self):
        """'Kodansha Comics USA' normalizes to 'Kodansha Comics'."""
        self.assertEqual(
            normalize_publisher_name('Kodansha Comics USA'),
            'Kodansha Comics'
        )

    def test_preset_name_unchanged(self):
        """A canonical preset name passes through unchanged."""
        self.assertEqual(
            normalize_publisher_name('Marvel'),
            'Marvel'
        )

    def test_unknown_name_unchanged(self):
        """An unknown publisher name passes through unchanged."""
        self.assertEqual(
            normalize_publisher_name('Random Press'),
            'Random Press'
        )

    def test_none_returns_none(self):
        """None input returns None."""
        self.assertIsNone(normalize_publisher_name(None))

    def test_empty_returns_empty(self):
        """Empty string returns empty string."""
        self.assertEqual(normalize_publisher_name(''), '')


if __name__ == '__main__':
    unittest.main()
