/*
 * Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
 */

#include "bgp/bgp_show_handler.h"

#include <boost/bind.hpp>
#include <boost/foreach.hpp>

#include "base/time_util.h"
#include "bgp/bgp_evpn.h"
#include "bgp/bgp_peer_internal_types.h"
#include "bgp/bgp_peer_types.h"
#include "bgp/bgp_server.h"
#include "bgp/bgp_table.h"
#include "bgp/evpn/evpn_table.h"
#include "bgp/routing-instance/routing_instance.h"

using std::string;
using std::vector;

//
// Used by EvpnManager::FillShowInfo for sorting.
// Note that the declaration is auto-generated by sandesh compiler.
//
bool ShowEvpnMcastLeaf::operator<(const ShowEvpnMcastLeaf &rhs) const {
    return get_address() < rhs.get_address();
}

//
// Fill in information for an evpn table.
//
static void FillEvpnTableInfo(ShowEvpnTable *sevt,
    const BgpSandeshContext *bsc, const EvpnTable *table, bool summary) {
    sevt->set_name(table->name());
    sevt->set_deleted(table->IsDeleted());
    sevt->set_deleted_at(
        UTCUsecToString(table->deleter()->delete_time_stamp_usecs()));
    sevt->set_mac_routes(table->mac_route_count());
    sevt->set_unique_mac_routes(table->unique_mac_route_count());
    sevt->set_im_routes(table->im_route_count());

    if (summary || table->IsVpnTable() || !table->GetEvpnManager())
        return;
    table->GetEvpnManager()->FillShowInfo(sevt);
}

//
// Fill in information for list of evpn tables.
//
// Allows regular and summary introspect to share code.
//
static bool FillEvpnTableInfoList(const BgpSandeshContext *bsc,
    bool summary, uint32_t page_limit, uint32_t iter_limit,
    const string &start_instance, const string &search_string,
    vector<ShowEvpnTable> *sevt_list, string *next_instance) {
    RoutingInstanceMgr *rim = bsc->bgp_server->routing_instance_mgr();
    RoutingInstanceMgr::const_name_iterator it =
        rim->name_clower_bound(start_instance);
    for (uint32_t iter_count = 0; it != rim->name_cend(); ++it, ++iter_count) {
        const RoutingInstance *rtinstance = it->second;
        const EvpnTable *table =
            static_cast<const EvpnTable *>(rtinstance->GetTable(Address::EVPN));
        if (!table)
            continue;
        if (!search_string.empty() &&
            (table->name().find(search_string) == string::npos) &&
            (search_string != "deleted" || !table->IsDeleted())) {
            continue;
        }
        ShowEvpnTable sevt;
        FillEvpnTableInfo(&sevt, bsc, table, summary);
        sevt_list->push_back(sevt);
        if (sevt_list->size() >= page_limit)
            break;
        if (iter_count >= iter_limit)
            break;
    }

    // All done if we've looked at all instances.
    if (it == rim->name_cend() || ++it == rim->name_end())
        return true;

    // Return true if we've reached the page limit, false if we've reached the
    // iteration limit.
    bool done = sevt_list->size() >= page_limit;
    *next_instance = it->second->name();
    return done;
}

//
// Specialization of BgpShowHandler<>::CallbackCommon for regular introspect.
//
template <>
bool BgpShowHandler<ShowEvpnTableReq, ShowEvpnTableReqIterate,
    ShowEvpnTableResp, ShowEvpnTable>::CallbackCommon(
    const BgpSandeshContext *bsc, Data *data) {
    uint32_t page_limit = bsc->page_limit() ? bsc->page_limit() : kPageLimit;
    uint32_t iter_limit = bsc->iter_limit() ? bsc->iter_limit() : kIterLimit;
    string next_instance;
    bool done = FillEvpnTableInfoList(bsc, false, page_limit, iter_limit,
        data->next_entry, data->search_string, &data->show_list,
        &next_instance);
    if (!next_instance.empty())
        SaveContextToData(next_instance, done, data);
    return done;
}

//
// Specialization of BgpShowHandler<>::FillShowList for regular introspect.
//
template <>
void BgpShowHandler<ShowEvpnTableReq, ShowEvpnTableReqIterate,
    ShowEvpnTableResp, ShowEvpnTable>::FillShowList(
    ShowEvpnTableResp *resp, const vector<ShowEvpnTable> &show_list) {
    resp->set_tables(show_list);
}

//
// Specialization of BgpShowHandler<>::CallbackCommon for summary introspect.
//
template <>
bool BgpShowHandler<ShowEvpnTableSummaryReq, ShowEvpnTableSummaryReqIterate,
    ShowEvpnTableSummaryResp, ShowEvpnTable>::CallbackCommon(
    const BgpSandeshContext *bsc, Data *data) {
    uint32_t page_limit = bsc->page_limit() ? bsc->page_limit() : kPageLimit;
    uint32_t iter_limit = bsc->iter_limit() ? bsc->iter_limit() : kIterLimit;
    string next_instance;
    bool done = FillEvpnTableInfoList(bsc, true, page_limit, iter_limit,
        data->next_entry, data->search_string, &data->show_list,
        &next_instance);
    if (!next_instance.empty())
        SaveContextToData(next_instance, done, data);
    return done;
}

//
// Specialization of BgpShowHandler<>::FillShowList for summary introspect.
//
template <>
void BgpShowHandler<ShowEvpnTableSummaryReq, ShowEvpnTableSummaryReqIterate,
    ShowEvpnTableSummaryResp, ShowEvpnTable>::FillShowList(
    ShowEvpnTableSummaryResp *resp, const vector<ShowEvpnTable> &show_list) {
    resp->set_tables(show_list);
}

//
// Handler for ShowEvpnTableReq.
// Schedules the callback to run in Task ("db::DBTable", 0) so that multicast
// data structures in EvpnManager can be accessed safely.
//
void ShowEvpnTableReq::HandleRequest() const {
    RequestPipeline::PipeSpec ps(this);
    RequestPipeline::StageSpec s1;
    TaskScheduler *scheduler = TaskScheduler::GetInstance();

    s1.taskId_ = scheduler->GetTaskId("db::DBTable");
    s1.cbFn_ = boost::bind(&BgpShowHandler<
        ShowEvpnTableReq,
        ShowEvpnTableReqIterate,
        ShowEvpnTableResp,
        ShowEvpnTable>::Callback, _1, _2, _3, _4, _5);
    s1.allocFn_ = BgpShowHandler<
        ShowEvpnTableReq,
        ShowEvpnTableReqIterate,
        ShowEvpnTableResp,
        ShowEvpnTable>::CreateData;
    s1.instances_.push_back(0);
    ps.stages_.push_back(s1);
    RequestPipeline rp(ps);
}

//
// Handler for ShowEvpnTableReqIterate.
// Schedules the callback to run in Task ("db::DBTable", 0) so that multicast
// data structures in EvpnManager can be accessed safely.
//
void ShowEvpnTableReqIterate::HandleRequest() const {
    RequestPipeline::PipeSpec ps(this);
    RequestPipeline::StageSpec s1;
    TaskScheduler *scheduler = TaskScheduler::GetInstance();

    s1.taskId_ = scheduler->GetTaskId("db::DBTable");
    s1.cbFn_ = boost::bind(&BgpShowHandler<
        ShowEvpnTableReq,
        ShowEvpnTableReqIterate,
        ShowEvpnTableResp,
        ShowEvpnTable>::CallbackIterate, _1, _2, _3, _4, _5);
    s1.allocFn_ = BgpShowHandler<
        ShowEvpnTableReq,
        ShowEvpnTableReqIterate,
        ShowEvpnTableResp,
        ShowEvpnTable>::CreateData;
    s1.instances_.push_back(0);
    ps.stages_.push_back(s1);
    RequestPipeline rp(ps);
}

//
// Handler for ShowEvpnTableSummaryReq.
// Schedules the callback to run in Task ("db::DBTable", 0) so that multicast
// data structures in EvpnManager can be accessed safely.  This is not really
// necessary for summary requests, but we do this for consistency with regular
// requests.
//
void ShowEvpnTableSummaryReq::HandleRequest() const {
    RequestPipeline::PipeSpec ps(this);
    RequestPipeline::StageSpec s1;
    TaskScheduler *scheduler = TaskScheduler::GetInstance();

    s1.taskId_ = scheduler->GetTaskId("db::DBTable");
    s1.cbFn_ = boost::bind(&BgpShowHandler<
        ShowEvpnTableSummaryReq,
        ShowEvpnTableSummaryReqIterate,
        ShowEvpnTableSummaryResp,
        ShowEvpnTable>::Callback, _1, _2, _3, _4, _5);
    s1.allocFn_ = BgpShowHandler<
        ShowEvpnTableSummaryReq,
        ShowEvpnTableSummaryReqIterate,
        ShowEvpnTableSummaryResp,
        ShowEvpnTable>::CreateData;
    s1.instances_.push_back(0);
    ps.stages_.push_back(s1);
    RequestPipeline rp(ps);
}

//
// Handler for ShowEvpnTableSummaryReqIterate.
// Schedules the callback to run in Task ("db::DBTable", 0) so that multicast
// data structures in EvpnManager can be accessed safely.  This is not really
// necessary for summary requests, but we do this for consistency with regular
// requests.
//
void ShowEvpnTableSummaryReqIterate::HandleRequest() const {
    RequestPipeline::PipeSpec ps(this);
    RequestPipeline::StageSpec s1;
    TaskScheduler *scheduler = TaskScheduler::GetInstance();

    s1.taskId_ = scheduler->GetTaskId("db::DBTable");
    s1.cbFn_ = boost::bind(&BgpShowHandler<
        ShowEvpnTableSummaryReq,
        ShowEvpnTableSummaryReqIterate,
        ShowEvpnTableSummaryResp,
        ShowEvpnTable>::CallbackIterate, _1, _2, _3, _4, _5);
    s1.allocFn_ = BgpShowHandler<
        ShowEvpnTableSummaryReq,
        ShowEvpnTableSummaryReqIterate,
        ShowEvpnTableSummaryResp,
        ShowEvpnTable>::CreateData;
    s1.instances_.push_back(0);
    ps.stages_.push_back(s1);
    RequestPipeline rp(ps);
}
