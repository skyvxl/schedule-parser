import unittest

import pdf_parser


class SlotMetadataTests(unittest.TestCase):
    def test_parse_entry_emits_explicit_slot_range(self) -> None:
        item = pdf_parser.parse_entry(
            "Защита информации Симонов М.Ф. Лекция. [02.09-28.10 ч.н.]",
            [("14:05", "15:40")],
            year=2026,
            sort_index=0,
            slot_start=3,
            slot_end=3,
        )

        payload = item.as_dict()

        self.assertEqual(payload["slot_start"], 3)
        self.assertEqual(payload["slot_end"], 3)

    def test_cell_slot_metadata_preserves_merged_column_span(self) -> None:
        header_cells = [
            (0.0, 0.0, 10.0, 5.0),
            (10.0, 0.0, 20.0, 5.0),
            (20.0, 0.0, 30.0, 5.0),
            (30.0, 0.0, 40.0, 5.0),
        ]
        time_ranges = [
            ("8:30", "10:05"),
            ("10:15", "11:50"),
            ("12:20", "13:55"),
            ("14:05", "15:40"),
        ]

        metadata = pdf_parser.cell_slot_metadata(
            header_cells,
            time_ranges,
            (10.0, 5.0, 30.0, 10.0),
        )

        self.assertEqual(
            metadata,
            (
                [("10:15", "11:50"), ("12:20", "13:55")],
                1,
                2,
            ),
        )


if __name__ == "__main__":
    unittest.main()
