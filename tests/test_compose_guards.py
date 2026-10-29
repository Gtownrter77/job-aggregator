"""Draft guards: wrong employer name, confirm_allow vs forbidden claims."""
import unittest

from aggregator.compose import (
    FORBIDDEN_CLAIMS,
    foreign_employer_problems,
    validate_draft,
)


class ForeignEmployer(unittest.TestCase):
    def test_flags_substituted_company(self):
        job = {"company": "Iugis Construction", "title": "Construction Project Accountant",
               "location": "Atlanta, GA", "description": ""}
        probs = foreign_employer_problems(
            "Excited about Construction Project Accountant at Eastern Commercial",
            "I am writing about the Construction Project Accountant role at Eastern Commercial. "
            "Eastern Commercial's focus on quality stands out.",
            job,
        )
        self.assertTrue(any("Eastern Commercial" in p for p in probs), probs)

    def test_allows_real_company(self):
        job = {"company": "Iugis Construction", "title": "PM", "location": "Atlanta, GA", "description": ""}
        probs = foreign_employer_problems(
            "Project Manager role at Iugis Construction",
            "I am interested in the Project Manager role at Iugis Construction in metro Atlanta.",
            job,
        )
        self.assertEqual(probs, [])

    def test_allows_first_token_match(self):
        job = {"company": "FOREVER Exteriors", "title": "Sales", "location": "Marietta, GA", "description": ""}
        probs = foreign_employer_problems(
            "Sales at FOREVER Exteriors",
            "Excited about sales at FOREVER.",
            job,
        )
        self.assertEqual(probs, [])


class ConfirmAllow(unittest.TestCase):
    def test_pmp_leed_16m_not_forbidden(self):
        # Ryan confirmed these; they are allowed via profile confirm_allow + removed from FORBIDDEN
        self.assertIsNone(FORBIDDEN_CLAIMS.search("I hold a PMP certification"))
        self.assertIsNone(FORBIDDEN_CLAIMS.search("LEED AP and Personally generated $16M"))
        self.assertIsNotNone(FORBIDDEN_CLAIMS.search("Ohio University graduate"))


if __name__ == "__main__":
    unittest.main()
