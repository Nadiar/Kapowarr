"""Searches queue their downloads straight away and record how each issue
fared, and a failed search is told apart from one that found nothing."""

import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

from backend.base.definitions import QueryResult, SearchAction
from backend.features.search_full import AutoSearchOutcome, SearchCoordinator
from backend.features.tasks import _search_and_queue
from backend.implementations.indexer_clients.ddl.GetComics import (
    GetComicsIndexer, _get_cached_search)
from backend.internals.db_models_search_stats import IssueSearchOutcome

VOLUME_ID = 7


def search_result(issue_number, link='https://gc/x', special_version=None):
    return {
        'link': link,
        'indexer_id': 1,
        'series': 'Series',
        'year': 2025,
        'volume_number': 1,
        'special_version': special_version,
        'issue_number': issue_number,
        'annual': False,
        'match': True
    }


class TestSearchAndQueue(unittest.TestCase):
    def setUp(self):
        self.outcome = patch(
            'backend.features.tasks.auto_search_outcome').start()
        handler = patch('backend.features.tasks.DownloadHandler').start()
        self.add_multiple = handler.return_value.add_multiple
        self.add_multiple.side_effect = lambda entries: list(entries)
        self.record_issues = patch(
            'backend.features.tasks.record_issue_searches').start()
        self.hit = patch('backend.features.tasks.record_search_hit').start()
        self.miss = patch('backend.features.tasks.record_search_miss').start()
        self.addCleanup(patch.stopall)

        self.task = MagicMock(stop=False)

    def search(self, results, errored=False, issue_id=None):
        self.outcome.return_value = AutoSearchOutcome(
            results=results,
            searched_issues=[(1, 1.0), (2, 2.0)],
            errored=errored
        )
        return _search_and_queue(self.task, VOLUME_ID, issue_id)

    def test_found_issue_is_queued_during_the_search(self):
        queued = self.search([search_result(2.0)])

        self.assertEqual(queued, 1)
        self.add_multiple.assert_called_once()
        self.record_issues.assert_called_once_with({
            1: IssueSearchOutcome.NOT_FOUND,
            2: IssueSearchOutcome.QUEUED
        })
        self.hit.assert_called_once_with(VOLUME_ID)
        self.miss.assert_not_called()

    def test_failed_search_is_not_a_miss(self):
        queued = self.search([], errored=True)

        self.assertEqual(queued, 0)
        self.record_issues.assert_called_once_with({
            1: IssueSearchOutcome.ERROR,
            2: IssueSearchOutcome.ERROR
        })
        self.hit.assert_not_called()
        self.miss.assert_not_called()

    def test_result_that_could_not_be_queued_is_found_not_queued(self):
        self.add_multiple.side_effect = lambda entries: []

        queued = self.search([search_result(2.0)])

        self.assertEqual(queued, 0)
        self.record_issues.assert_called_once_with({
            1: IssueSearchOutcome.NOT_FOUND,
            2: IssueSearchOutcome.FOUND
        })
        self.miss.assert_called_once_with(VOLUME_ID)
        self.hit.assert_not_called()

    def test_range_result_covers_every_issue_in_it(self):
        self.search([search_result((1.0, 2.0))])

        self.record_issues.assert_called_once_with({
            1: IssueSearchOutcome.QUEUED,
            2: IssueSearchOutcome.QUEUED
        })

    def test_special_version_result_covers_every_searched_issue(self):
        self.search([search_result(None, special_version='tpb')])

        self.record_issues.assert_called_once_with({
            1: IssueSearchOutcome.QUEUED,
            2: IssueSearchOutcome.QUEUED
        })

    def test_yields_before_each_download_and_stops_when_asked(self):
        def stop_after_first_yield():
            self.task.stop = True

        self.task.check_yield.side_effect = stop_after_first_yield

        queued = self.search([
            search_result(1.0, link='https://gc/1'),
            search_result(2.0, link='https://gc/2')
        ])

        self.task.check_yield.assert_called_once()
        self.add_multiple.assert_not_called()
        self.assertEqual(queued, 0)

    def test_issue_search_passes_issue_id_to_downloads(self):
        self.search([search_result(2.0)], issue_id=2)

        entries = list(self.add_multiple.call_args.args[0])
        self.assertEqual(entries[0][4], 2)


class TestSearchErrorsAreReported(unittest.TestCase):
    def test_coordinator_notes_an_indexer_that_failed(self):
        coordinator = SearchCoordinator.__new__(SearchCoordinator)
        coordinator.errored = False
        indexer = MagicMock()
        indexer.search = AsyncMock(
            return_value=QueryResult([], False, errored=True)
        )
        planner = MagicMock()
        planner.next_action.return_value = (SearchAction.SEARCH_VOLUME, [])
        coordinator.indexers = [{
            'indexer': indexer,
            'query_builder': MagicMock(),
            'search_action_planner': planner
        }]

        asyncio.run(coordinator._run_iteration())

        self.assertTrue(coordinator.errored)

    def test_getcomics_page_that_failed_to_load_is_an_error_not_cached(self):
        indexer = GetComicsIndexer.__new__(GetComicsIndexer)
        indexer._url = 'https://getcomics.test'
        indexer._id = 1
        indexer._title = 'GetComics'
        indexer.request_count = 0
        indexer.session = MagicMock()
        indexer.session.get_text = AsyncMock(return_value='')
        query = {'query': 'failing query', 'page': 1,
                 'total_available_variations': 1}

        result = asyncio.run(indexer.search(query))  # type: ignore

        self.assertTrue(result.errored)
        self.assertIsNone(_get_cached_search(
            ('https://getcomics.test', 'failing query', 1)
        ))


if __name__ == '__main__':
    unittest.main()
