# mapapi.py: map data api for map-navigator

import os
import json
import uuid
import logging

from PIL import ImageColor
from collections import Counter

from uuid import UUID
from datetime import date
from datetime import datetime

import numpy as np
import datajoint as dj
import pathlib
from decimal import Decimal

from flask import Flask
from flask import request
from flask import abort

import boto3
s3_client = boto3.client('s3')

API_VERSION = '0'
app = Flask(__name__)
API_PREFIX = '/v{}'.format(API_VERSION)
is_gunicorn = "gunicorn" in os.environ.get("SERVER_SOFTWARE", "")

os.environ['DJ_SUPPORT_FILEPATH_MANAGEMENT'] = "TRUE"


def mkvmod(mod):
    return dj.create_virtual_module(mod, 'map_v2_{}'.format(mod))


lab = mkvmod('lab')
experiment = mkvmod('experiment')
ephys = mkvmod('ephys')
psth = mkvmod('psth')
report = mkvmod('report')
tracking = mkvmod('tracking')
histology = mkvmod('histology')
ccf = mkvmod('ccf')

map_s3_bucket = os.environ.get('MAP_S3_BUCKET')
map_store_location = os.environ.get('MAP_REPORT_STORE_LOCATION')
map_store_stage = os.environ.get('MAP_REPORT_STORE_STAGE')
dj.config['stores'] = {
    'report_store': dict(
      protocol='s3',
      endpoint='s3.amazonaws.com',
      access_key=os.environ.get('AWS_ACCESS_KEY_ID'),
      secret_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
      bucket=map_s3_bucket,
      location=map_store_location,
      stage=map_store_stage
    )
}


class DateTimeEncoder(json.JSONEncoder):
    ''' teach json to dump datetimes, etc '''

    npmap = {
        np.bool_: bool,
        np.uint8: str,
        np.uint16: str,
        np.uint32: str,
        np.uint64: str,
        np.int8: str,
        np.int16: str,
        np.int32: str,
        np.int64: str,
        np.float32: str,
        np.float64: str,
        np.ndarray: list,
        Decimal: float
    }

    def default(self, o):
        if isinstance(o, date):
            return o.isoformat()
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, uuid.UUID):
            return str(o)
        if type(o) in self.npmap:
            return self.npmap[type(o)](o)
        return json.JSONEncoder.default(self, o)

    @classmethod
    def dumps(cls, obj):
        return json.dumps(obj, cls=cls)


# _start:

reqmap = {
    '_q': None,
    'subject': lab.Subject
}
dumps = DateTimeEncoder.dumps


def mkpath(path):
    return '{}{}'.format(API_PREFIX, path)


@app.route(mkpath('/<path:subpath>'), methods=['GET', 'POST'])
def do_req(subpath):
    app.logger.info("method: '{}', path: {}, values: {}".format(
        request.method, request.path, request.values))

    # 1) parse request & arguments
    pathparts = request.path.split('/')[2:]  # ['', 'v0'] [ ... ]
    obj = pathparts[0]

    values = request.values
    postargs, jsonargs = {}, None

    limit = int(request.values['__limit']) if '__limit' in values else None
    order = request.values['__order'] if '__order' in values else None
    proj = json.loads(request.values['__proj']) if '__proj' in values else None

    special_fields = ['__json', '__limit', '__order', '__proj']
    for a in (v for v in values if v not in special_fields):
        # HACK: 'uuid' attrs -> UUID type (see also: datajoint-python #594)
        postargs[a] = UUID(values[a]) if 'uuid' in a else values[a]

    args = [postargs] if len(postargs) else []
    if '__json' in values:
        jsonargs = json.loads(request.values['__json'])
        args += jsonargs if type(jsonargs) == list else [jsonargs]

    args = {} if not args else dj.AndList(args)
    kwargs = {i[0]: i[1] for i in (('as_dict', True,),
                                   ('limit', limit,),
                                   ('order_by', order,)) if i[1] is not None}

    # 2) and dispatch
    app.logger.debug("args: '{}', kwargs: {}".format(args, kwargs))
    if obj not in reqmap:
        abort(404)
    elif obj == '_q':
        return handle_q(pathparts[1], args, proj, **kwargs)
    else:
        q = (reqmap[obj] & args)
        if proj:
            q = q.proj(*proj)

        from time import time
        start = time()
        print('about to fetch requested object')
        print(start)
        fetched = q.fetch(**kwargs)
        dur = time() - start
        print('Took {} seconds to fetch dataset'.format(dur))
        return dumps(fetched)
        # return dumps(q.fetch(**kwargs))


def handle_q(subpath, args, proj, **kwargs):
    '''
    special queries (under '/_q/ URL Space)
      - for sessionpage, provide:
        ((session * subject * lab * user) & arg).proj(flist)
    '''
    app.logger.info("handle_q: subpath: '{}', args: {}".format(subpath, args))

    # ---------- process the "args" ----------
    if isinstance(args, list):
      if len(args) == 1:
        args = args[0]
      else:
        raise ValueError(f'args is a list of multiple dicts: {args}')

    contain_s3fp = False
    if subpath == 'sessionpage':

        sessions = (experiment.Session * lab.WaterRestriction).aggr(
          ephys.ProbeInsertion, 'water_restriction_number', 'username',
          session_date="cast(concat(session_date, ' ', session_time) as datetime)",
          probe_count='count(insertion_number)', keep_all_rows=True)

        sessions = sessions.aggr(ephys.ProbeInsertion.RecordableBrainRegion.proj(
          brain_region='CONCAT(hemisphere, " ", brain_area)'), ...,
          insert_locations='GROUP_CONCAT(brain_region SEPARATOR", ")', keep_all_rows=True)

        sessions = sessions.aggr(tracking.Tracking, ..., tracking_avai='count(trial) > 0', keep_all_rows=True)

        unitsessions = experiment.Session.proj().aggr(ephys.Unit.proj(), ..., clustering_methods='GROUP_CONCAT(DISTINCT clustering_method SEPARATOR", ")', keep_all_rows=True)
        unitsessions = unitsessions.aggr(ephys.ClusteringLabel, ..., quality_control='SUM(quality_control) > 0',
                                         manual_curation='SUM(manual_curation) > 0', keep_all_rows=True).proj(
          ..., quality_control='IFNULL(quality_control, false)', manual_curation='IFNULL(manual_curation, false)')
        unitsessions = unitsessions.aggr(histology.ElectrodeCCFPosition, ..., histology_avai='count(insertion_number) > 0', keep_all_rows=True)

        plotsessions = experiment.Session.proj().aggr(report.SessionLevelReport, ..., behavior_performance_s3fp='behavior_performance', keep_all_rows=True)
        plotsessions = plotsessions.aggr(report.SessionLevelProbeTrack, ..., session_tracks_plot_s3fp='session_tracks_plot', keep_all_rows=True)
        plotsessions = plotsessions.aggr(report.SessionLevelCDReport, ..., coding_direction_s3fp='coding_direction', keep_all_rows=True)

        sessions = sessions * unitsessions * plotsessions

        # handling special GROUPCONCAT attributes: `insert_locations` and `clustering_methods` in args
        insert_locations_restr = make_LIKE_restrictor('insert_locations', args,
                                                      (ephys.ProbeInsertion.RecordableBrainRegion.proj(
                                                        brain_region='CONCAT(hemisphere, " ", brain_area)'),
                                                       'brain_region'))
        clustering_methods_restr = make_LIKE_restrictor('clustering_methods', args,
                                                        (ephys.ClusteringMethod, 'clustering_method'))
        [args.pop(v) for v in ('insert_locations', 'clustering_methods') if v in args]

        contain_s3fp = True
        q = sessions & args & insert_locations_restr & clustering_methods_restr
    elif subpath == 'probe_insertions':
        exclude_attrs = ['-electrode_config_name']
        probe_insertions = (ephys.ProbeInsertion * ephys.ProbeInsertion.InsertionLocation).proj(
          ..., *exclude_attrs).aggr(ephys.ProbeInsertion.RecordableBrainRegion.proj(
          brain_region='CONCAT(hemisphere, " ", brain_area)'), ...,
          brain_regions='GROUP_CONCAT(brain_region SEPARATOR", ")', keep_all_rows=True)

        probe_insertions = probe_insertions.aggr(ephys.ClusteringLabel, ..., quality_control='SUM(quality_control) > 0',
                                                 manual_curation='SUM(manual_curation) > 0', keep_all_rows=True).proj(
          ..., quality_control='IFNULL(quality_control, false)', manual_curation='IFNULL(manual_curation, false)')

        probe_insertions = probe_insertions & args
        probe_insertions = probe_insertions.aggr(
          report.ProbeLevelReport, ..., clustering_quality_s3fp='clustering_quality',
          unit_characteristic_s3fp='unit_characteristic', group_psth_s3fp='group_psth', keep_all_rows=True)
        probe_insertions = probe_insertions.aggr(report.ProbeLevelPhotostimEffectReport, ...,
                                                 group_photostim_s3fp='group_photostim', keep_all_rows=True)

        contain_s3fp = True
        q = probe_insertions
    elif subpath == 'units':
        exclude_attrs = ['-spike_times', '-waveform', '-unit_uid', '-spike_depths', '-spike_sites',
                         '-probe', '-electrode_config_name', '-electrode_group']
        units = (ephys.Unit * ephys.UnitStat
                 * ephys.ProbeInsertion.InsertionLocation.proj('depth') & args).proj(
          ..., unit_depth='unit_posy + depth', is_all='unit_quality = "all"', *exclude_attrs)

        units = units.aggr(report.UnitLevelEphysReport, ..., unit_psth_s3fp='unit_psth', keep_all_rows=True)
        units = units.aggr(report.UnitLevelTrackingReport, ..., unit_behavior_s3fp='unit_behavior', keep_all_rows=True)

        contain_s3fp = True
        q = units
    elif subpath == 'project_probe_tracks':
        args['project_name'] = 'MAP'
        contain_s3fp = True
        q = report.ProjectLevelProbeTrack.proj(tracks_plot_s3fp='tracks_plot') & args
    else:
        abort(404)

    if isinstance(q, (list, dict)):
        ret = q
    else:
        if proj:
            ret = q.proj(*proj).fetch(**kwargs)
        else:
            ret = q.fetch(**kwargs)

    # print('D type', ret.dtype)
    # print(ret)
    print('About to return ', len(ret), 'entries')
    app.logger.info("About to return {} entries".format(len(ret)))
    return dumps(post_process(ret)) if contain_s3fp else dumps(ret)

# ----------- HELPER METHODS -------------------


def make_presign_url(data_link):
    return s3_client.generate_presigned_url(
      'get_object',
      Params={'Bucket': map_s3_bucket,
              'Key': data_link,
              'ResponseContentType': 'image/png'},
      ExpiresIn=3 * 60 * 60)


def convert_to_s3_path(local_path):
    local_path = pathlib.Path(local_path)
    rel_path = local_path.relative_to(pathlib.Path(map_store_stage))
    return (pathlib.Path(map_store_location) / rel_path).as_posix()


def post_process(ret):
    return [{k.replace('_s3fp', ''): make_presign_url(convert_to_s3_path(v)) if '_s3fp' in k and v else v
             for k, v in i.items()} for i in ret]


def make_LIKE_restrictor(attr_name, restriction_dict, lookup_table=None):
    if attr_name in restriction_dict:
        attr_value = restriction_dict[attr_name]
        if lookup_table is not None:
            tbl, lookup_attr = lookup_table
            if not (tbl & {lookup_attr: attr_value}):
                return {}
            return f'{attr_name} LIKE "%{attr_value}%"'
    return {}

# --------------------------------


if is_gunicorn:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)

if __name__ == '__main__':
    app.run(host='0.0.0.0')
