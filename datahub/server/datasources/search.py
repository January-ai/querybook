# TODO: refactor
import re
import json


from app.auth.permission import (
    verify_environment_permission,
    verify_metastore_permission,
)
from app.datasource import register, api_assert
from lib.logger import get_logger
from logic.elasticsearch import ES_CONFIG, get_hosted_es


LOG = get_logger(__file__)


def _highlight_fields(fields_to_highlight):
    return {
        "highlight": {
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
            "type": "plain",
            "fields": fields_to_highlight,
        }
    }


def _match_any_field(keywords="", search_fields=[]):
    if keywords == "":
        return {}
    query = {
        "multi_match": {
            "query": keywords,
            "fields": search_fields,
            "type": "cross_fields",
            "minimum_should_match": "100%",
        }
    }
    return query


def _match_filters(filters):
    if not filters:
        return {}

    filter_terms = []
    range_filters = {}

    for f in filters:
        field_name = str(f[0]).lower()
        field_val = str(f[1]).lower()

        if not field_val or field_val == "":
            continue

        field = {}
        field[field_name] = field_val

        if field_name == "startdate":
            range_filters.setdefault("created_at", {"gte": field_val})
        elif field_name == "enddate":
            range_filters.setdefault("created_at", {"lte": field_val})
        else:
            filter_terms.append({"match": field})

    if any(range_filters):
        return {"filter": {"bool": {"must": filter_terms}}, "range": range_filters}
    else:
        return {"filter": {"bool": {"must": filter_terms}}}


def _construct_datadoc_query(
    keywords, filters, limit, offset, sort_key=None, sort_order=None,
):
    search_query = _match_any_field(
        keywords, search_fields=["title^5", "cells", "owner",]
    )
    search_filter = _match_filters(filters)

    bool_query = {}
    if search_query != {}:
        bool_query["must"] = [search_query]
    if search_filter != {}:
        bool_query["filter"] = search_filter["filter"]
        if "range" in search_filter:
            bool_query["must"].append({"range": search_filter["range"]})

    query = {
        "query": {"bool": bool_query},
        "_source": ["id", "title", "owner_uid", "created_at"],
        "size": limit,
        "from": offset,
    }

    if sort_key:
        if not isinstance(sort_key, list):
            sort_key = [sort_key]
            sort_order = [sort_order]
        sort_query = [
            {val: {"order": order}} for order, val in zip(sort_order, sort_key)
        ]

        query.update({"sort": sort_query})
    query.update(
        _highlight_fields({"cells": {"fragment_size": 60, "number_of_fragments": 3,}})
    )

    return json.dumps(query)


def _construct_tables_query(
    keywords, filters, limit, offset, concise, sort_key=None, sort_order=None,
):

    search_query = {}
    if keywords:
        search_query["multi_match"] = {
            "query": keywords,
            "fields": ["full_name^20", "columns", "description"],
            "minimum_should_match": -1,
        }
    else:
        search_query["match_all"] = {}

    search_query = {
        "function_score": {
            "query": search_query,
            "boost_mode": "multiply",
            "script_score": {
                "script": {
                    "source": "doc['importance_score'].value + (doc['golden'].value ? 2 : 0)"
                }
            },
        }
    }

    search_filter = _match_filters(filters)

    bool_query = {}
    if search_query != {}:
        bool_query["must"] = [search_query]
    if search_filter != {}:
        bool_query["filter"] = search_filter["filter"]
        if "range" in search_filter:
            bool_query["must"].append({"range": search_filter["range"]})

    query = {
        "query": {"bool": bool_query},
        "size": limit,
        "from": offset,
    }

    if concise:
        query["_source"] = ["id", "schema", "name"]

    if sort_key:
        if not isinstance(sort_key, list):
            sort_key = [sort_key]
            sort_order = [sort_order]
        sort_query = [
            {val: {"order": order}} for order, val in zip(sort_order, sort_key)
        ]

        query.update({"sort": sort_query})
    query.update(
        _highlight_fields(
            {
                "columns": {"fragment_size": 20, "number_of_fragments": 5,},
                "description": {"fragment_size": 60, "number_of_fragments": 3,},
            }
        )
    )

    return json.dumps(query)


def _parse_results(results, get_count):
    def extract_hits(results):
        return results.get("hits", {}).get("hits", [])

    ret = []
    elements = extract_hits(results)
    for element in elements:
        r = element.get("_source", {})
        if element.get("highlight"):
            r.update({"highlight": element.get("highlight")})
        ret.append(r)

    if get_count:
        total_found = results.get("hits", {}).get("total", 0)
        return ret, total_found

    return ret


def _get_matching_objects(query, index_name, doc_type, get_count=False):
    result = None
    try:
        result = get_hosted_es().search(index_name, doc_type, body=query)
    except Exception as e:
        LOG.warning("Got ElasticSearch exception: \n " + str(e))

    if result is None:
        LOG.debug("No Elasticsearch attempt succeeded")
        result = {}
    return _parse_results(result, get_count)


@register("/search/datadoc/", methods=["GET"])
def search_datadoc(
    environment_id,
    keywords,
    filters=[],
    sort_key=None,
    sort_order=None,
    limit=1000,
    offset=0,
):
    verify_environment_permission([environment_id])
    filters.append(["environment_id", environment_id])
    # Unfortuantely currently we can't search including underscore,
    # so split. # TODO: Allow for both.
    # parsed_keywords = map(lambda x: re.split('(-|_)', x), keywords)
    query = _construct_datadoc_query(
        keywords=keywords,
        filters=filters,
        limit=limit,
        offset=offset,
        sort_key=sort_key,
        sort_order=sort_order,
    )

    # print query
    results, count = _get_matching_objects(
        query,
        ES_CONFIG["datadocs"]["index_name"],
        ES_CONFIG["datadocs"]["type_name"],
        True,
    )
    return {"count": count, "data": results}


@register("/search/tables/", methods=["GET"])
def search_tables(
    metastore_id,
    keywords,
    filters=[],
    sort_key=None,
    sort_order=None,
    limit=1000,
    offset=0,
    concise=False,
):
    verify_metastore_permission(metastore_id)
    filters.append(["metastore_id", metastore_id])
    # Unfortuantely currently we can't search including underscore,
    # so split. # TODO: Allow for both.
    parsed_keywords = " ".join(re.split("-|_|\\.", keywords))
    query = _construct_tables_query(
        keywords=parsed_keywords,
        filters=filters,
        limit=limit,
        offset=offset,
        concise=concise,
        sort_key=sort_key,
        sort_order=sort_order,
    )

    results, count = _get_matching_objects(
        query,
        ES_CONFIG["tables"]["index_name"],
        ES_CONFIG["tables"]["type_name"],
        True,
    )
    return {"count": count, "data": results}


@register("/suggest/<int:metastore_id>/tables/", methods=["GET"])
def suggest_tables(metastore_id, prefix, limit=10):
    verify_metastore_permission(metastore_id)
    # Unfortuantely currently we can't search including underscore,
    # so split. # TODO: Allow for both.
    # parsed_keywords = map(lambda x: re.split('(-|_)', x), keywords)
    query = {
        "suggest": {
            "suggest": {
                "text": prefix,
                "completion": {
                    "field": "completion_name",
                    "size": limit,
                    "contexts": {"metastore_id": metastore_id},
                },
            }
        },
    }

    index_name = ES_CONFIG["tables"]["index_name"]
    type_name = ES_CONFIG["tables"]["type_name"]

    result = None
    try:
        # print '\n--ES latest hosted_index %s\n' % hosted_index
        result = get_hosted_es().search(index_name, type_name, body=query)
    except Exception as e:
        LOG.info(e)
    finally:
        if result is None:
            result = {}
    options = next(iter(result.get("suggest", {}).get("suggest", [])), {}).get(
        "options", []
    )
    texts = [
        "{}.{}".format(
            option.get("_source", {}).get("schema", ""),
            option.get("_source", {}).get("name", ""),
        )
        for option in options
    ]
    return {"data": texts}


# /search/ but it is actually suggest
@register("/search/user/", methods=["GET"])
def suggest_user(name, limit=10, offset=None):
    api_assert(limit is None or limit <= 100, "Requesting too many users")

    query = {
        "suggest": {
            "suggest": {
                "text": (name or "").lower(),
                "completion": {"field": "suggest", "size": limit},
            }
        },
    }

    index_name = ES_CONFIG["users"]["index_name"]
    type_name = ES_CONFIG["users"]["type_name"]

    result = None
    try:
        # print '\n--ES latest hosted_index %s\n' % hosted_index
        result = get_hosted_es().search(index_name, type_name, body=query)
    except Exception as e:
        LOG.info(e)
    finally:
        if result is None:
            result = {}

    options = next(iter(result.get("suggest", {}).get("suggest", [])), {}).get(
        "options", []
    )
    users = [
        {
            "id": option.get("_source", {}).get("id"),
            "username": option.get("_source", {}).get("username"),
            "fullname": option.get("_source", {}).get("fullname"),
        }
        for option in options
    ]
    return users
