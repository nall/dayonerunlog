#!/usr/bin/env python
# vim: ft=python expandtab softtabstop=0 tabstop=4 shiftwidth=4
# 
# Copyright (c) 2016, Jon Nall 
# All rights reserved. 
# 
# Redistribution and use in source and binary forms, with or without 
# modification, are permitted provided that the following conditions are met: 
# 
#  * Redistributions of source code must retain the above copyright notice, 
#    this list of conditions and the following disclaimer. 
#  * Redistributions in binary form must reproduce the above copyright 
#    notice, this list of conditions and the following disclaimer in the 
#    documentation and/or other materials provided with the distribution. 
#  * Neither the name of  nor the names of its contributors may be used to 
#    endorse or promote products derived from this software without specific 
#    prior written permission. 
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" 
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE 
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE 
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE 
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR 
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF 
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS 
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN 
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) 
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE 
# POSSIBILITY OF SUCH DAMAGE. 

# FIXME: Remove get_activity_photos workarounds if stravalib gets fixed
# FIXME: Remove buggy_start workaroungs if smashrun-client gets fixed
# FIXME: Bug when there are two runs on the same day a badge is required -- I think both will get it

import argparse
import dateutil
import functools
import logging
import pprint
import os
import requests
import stravalib
import subprocess
import sys
import tempfile
import urllib
import yaml

from smashrun.client import Smashrun
from stravalib.client import Client
from datetime import date
from datetime import datetime
from dateutil.tz import tzoffset
from pint import UnitRegistry


def custom_title(run):
    notes = run['notes']
    prefix = '::Location='
    for line in notes.splitlines():
        if line.startswith(prefix):
            title = line[len(prefix):].strip()
            if title != '':
                return title
    return default_title(run)


CFG_START_TIME_THRESHOLD_IN_SECS = 90
CFG_DISTANCE_THRESHOLD_IN_METERS = 150
CFG_STRAVA_PHOTO_SIZE = 1000
CFG_ACTIVITY_TITLE_FN = custom_title  # For an example, look at the custom_title def


# Generally nothing below this line should be changed
UNITS = UnitRegistry()
STATE_FILE_PREFIX = 'LastUpdateStop: '
STATE_FILE_TIME_FORMAT = '%Y-%m-%dT%H:%M:%S'  # this is in local time


def parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('--credentials_file', type=str, required=True, help='The name of the file holding service credentials')
    parser.add_argument('--journal',          type=str,                help='The name of the DayOne journal to use')
    parser.add_argument('--start',            type=str,                help='An initial start date of the form YYYY-MM-DD[THH:MM:SS]')
    parser.add_argument('--stop',             type=str,                help='An end date of the form YYYY-MM-DD[THH:MM:SS]')
    parser.add_argument('--days',             type=int,                help='Number of days since start to process')
    parser.add_argument('--tag', dest='tags', type=str, default=[],    action='append', help='Number of days since start to process')    # noqa
    parser.add_argument('--state_file',       type=str,                help='Request all runs since state file\'s modification time and update time afterwards')  # noqa
    parser.add_argument('--create_state_file', action='store_true', help='Create a state file based on this invocation')
    parser.add_argument('--no_coordinates',    action='store_true', help='Do not attempt to set coordinates for the entry')
    parser.add_argument('--no_strava',         action='store_true', help='Do not query Strava for photos or run routes')
    parser.add_argument('--no_badges',         action='store_true', help='Do not query SmasRun for badges')
    parser.add_argument('--no_route',          action='store_true', help='Do not query SmasRun for badges')
    parser.add_argument('--dryrun',            action='store_true', help='Do not create journal entries. Just print the CLI commands to do so')  # noqa
    parser.add_argument('--debug',             action='store_true', help='Enable verbose debug')
    args = parser.parse_args()

    if not os.path.isfile(args.credentials_file):
        parser.error('No such credentials file: %s' % (args.credentials_file))
    if args.state_file and not os.path.isfile(args.state_file):
        parser.error('No such state file: %s' % (args.state_file))
    if args.state_file and args.start and not args.create_state_file:
        parser.error("Must specify at most one of --start and --state_file")
    if args.state_file and args.days and not args.create_state_file:
        parser.error("Must specify at most one of --state_file and --days")
    if args.stop and args.days:
        parser.error("Must specify at most one of --stop and --days")

    with open(args.credentials_file, 'r') as fh:
        setattr(args, 'credentials', yaml.load(fh))
        args.credentials.setdefault('smashrun', None)
        args.credentials.setdefault('strava', None)
        args.credentials.setdefault('google_maps_apikey', None)

    return args


def setup(argv):
    args = parse_args(argv)
    logging.basicConfig(filename='dayonerunlog.log', level=logging.DEBUG if args.debug else logging.INFO)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if args.debug else logging.INFO)
    formatter = logging.Formatter('%(levelname)-8s %(message)s')
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)

    to_zone = dateutil.tz.tzlocal()

    if args.start is None:
        if args.state_file and not args.create_state_file:
            tstamp = None
            with open(args.state_file, 'r') as fh:
                for line in fh.readlines():
                    if line.startswith(STATE_FILE_PREFIX):
                        tstamp = line[len(STATE_FILE_PREFIX):].rstrip()
                        break
            assert tstamp is not None, "Unable to determine last update time from %s" % (args.state_file)
            args.start = datetime.strptime(tstamp, STATE_FILE_TIME_FORMAT)
        else:
            # Use yesterday
            args.start = date.fromordinal(date.today().toordinal() - 1)
            args.start = datetime.combine(args.start, datetime.min.time())
    else:
        fmt = '%Y-%m-%dT%H:%M:%S' if args.start.find('T') != -1 else '%Y-%m-%d'
        args.start = datetime.strptime(args.start, fmt)
    args.start = args.start.replace(tzinfo=to_zone)

    if args.stop is None:
        if args.days is None:
            args.stop = datetime.now()
        else:
            args.stop = date.fromordinal(start.toordinal() + args.days)
            args.stop = datetime.combine(args.stop, datetime.max.time())
    else:
        fmt = '%Y-%m-%dT%H:%M:%S' if args.stop.find('T') != -1 else '%Y-%m-%d'
        args.stop = datetime.strptime(args.stop, fmt)

    args.stop = args.stop.replace(tzinfo=to_zone)

    return args


def time_string(pace):
    SECS_PER_SEC = 1.0
    SECS_PER_MIN = SECS_PER_SEC * 60.0
    SECS_PER_HOUR = SECS_PER_MIN * 60.0
    hours = int(pace / SECS_PER_HOUR)
    secs_left = (pace - (hours * SECS_PER_HOUR))
    minutes = int(secs_left / SECS_PER_MIN)
    secs_left = int((secs_left - (minutes * SECS_PER_MIN)))

    s = ''
    if hours > 0:
        s += '%02d:' % (hours)
    if hours > 0 or minutes > 0:
        s += '%02d:' % (minutes)
    s += '%02d' % (secs_left)

    return s


def download_url(url):
    r = requests.get(url)
    if r.status_code == 200:
        with tempfile.NamedTemporaryFile(prefix='dayonerun_strava_photo_', delete=False) as fh:
            fh.write(r.content)
            return fh.name
    else:
        logging.warning("Unable to download %s: %s" % (url, r.text))
        return None


def strava_client(client_id=None, client_secret=None, refresh_token=None, access_token=None):
    client = Client()
    if access_token is None:
        authorize_url = client.authorization_url(client_id=client_id,
                                                 redirect_uri='http://localhost:8282/authorized',
                                                 scope='view_private,write')
        logging.info("Go to %s" % (authorize_url))
        code = raw_input("Code: ")
        client.access_token = client.exchange_code_for_token(client_id=client_id,
                                                             client_secret=client_secret,
                                                             code=code)
        logging.info("Access Token = %s" % (client.access_token))
    else:
        client.access_token = access_token

    return client


def st_get_photos(strava, activity_id):
    # WORKAROUND until stravalib.get_activity_photos is fixed to include photo_sources
    result_fetcher = functools.partial(strava.protocol.get,
                                       '/activities/{id}/photos',
                                       id=activity_id, photo_sources=True, size=CFG_STRAVA_PHOTO_SIZE)

    return stravalib.client.BatchedResultsIterator(entity=stravalib.model.ActivityPhoto,
                                                   bind_client=strava,
                                                   result_fetcher=result_fetcher)


def st_get_runs(strava, start, stop):
    logging.info("Retriving Strava Runs START: %s" % (start))
    logging.info("                       STOP: %s" % (stop))

    activities = []
    for activity in strava.get_activities(after=start, before=stop):
        raw = strava.protocol.get('/activities/{id}', id=activity.id, include_all_efforts=True)
        activities.append(raw)
        logging.debug("STRAVA_ACTIVITY(%s)=%s" % (activity.id, pprint.pformat(raw)))
    return activities


def st_find_strava_run(sr_run, st_runs):
    from_zone = dateutil.tz.tzutc()
    to_zone = dateutil.tz.tzlocal()

    for st_run in st_runs:
        # 2016-11-17T15:59:56Z
        utc = datetime.strptime(st_run['start_date'], '%Y-%m-%dT%H:%M:%SZ')
        utc = utc.replace(tzinfo=from_zone)
        local = utc.astimezone(to_zone)
        st_time = local
        st_distance = float(st_run['distance'])
        sr_time = sr_run['__localtime']
        sr_distance = float(sr_run['distance'] * 1000.0)
        logging.debug("STRAVA: %s (%s) %s" % (st_time, local, st_distance))
        logging.debug("SRUN  : %s (%s) %s" % (sr_time, sr_run['__localtime'], sr_distance))
        logging.debug("   TIME (%s) (max: %s)" % (abs(st_time - sr_time), CFG_START_TIME_THRESHOLD_IN_SECS))
        logging.debug("   DIST (%s) (max: %s)" % (abs(st_distance - sr_distance), CFG_DISTANCE_THRESHOLD_IN_METERS))
        if abs(st_time - sr_time).total_seconds() < CFG_START_TIME_THRESHOLD_IN_SECS:
            if abs(st_distance - sr_distance) < CFG_DISTANCE_THRESHOLD_IN_METERS:
                return st_run

    return None


def st_append_strava_info(strava, sr_run, st_runs, args, google_maps_apikey=None):
    st_run = st_find_strava_run(sr_run, st_runs)
    if st_run is None:
        logging.warning("Found no Strava run corresponding to SmashRun activity %s" % (sr_run['__id']['smashrun']))
        return
    logging.info("Found Strava activity %s that matches SmashRun activity %s" % (st_run['id'], sr_run['__id']['smashrun']))

    sr_run['__id']['strava'] = st_run['id']
    sr_run['__tags'].append('strava')
    sr_run['__activity_urls']['strava'] = 'https://www.strava.com/activities/%s' % (st_run['id'])

    # Add Strava route from polyline
    polyline = None
    if 'map' in st_run and 'polyline' in st_run['map']:
        polyline = st_run['map']['polyline']
    if not args.no_route and google_maps_apikey is not None and polyline:
        poly = urllib.quote(polyline)
        url = 'https://maps.googleapis.com/maps/api/staticmap?size=640x640&path=weight:6%%7Ccolor:blue%%7Cenc:%s&key=%s' % (poly, google_maps_apikey)  # noqa
        fname = download_url(url)
        if fname is not None:
            sr_run['__photos'].append(fname)

    # Add any Strava photos
    logging.info("Getting any photos for %s" % (st_run['id']))
    for photo in st_get_photos(strava, st_run['id']):
        logging.debug("PHOTO: %s" % pprint.pformat(photo))
        logging.debug("        ref : %s" % (photo.ref))
        logging.debug("        urls: %s" % (pprint.pformat(photo.urls)))
        fname = download_url(photo.urls[str(CFG_STRAVA_PHOTO_SIZE)])
        if fname is not None:
            sr_run['__photos'].append(fname)


def smashrun_client(client_id=None, client_secret=None, refresh_token=None, access_token=None):
    if client_id is None:
        raise ValueError("Must specify a valid client_id")
    if client_secret is None:
        raise ValueError("Must specify a valid client_secret")

    if refresh_token is None:
        raise RuntimeError("Must supply a token currently")
    else:
        client = Smashrun(client_id=client_id, client_secret=client_secret)
        client.refresh_token(refresh_token=refresh_token)
        return client


def sr_get_split_info(details, split_interval=1.0 * UNITS.mile):
    indices = {}
    idx = 0
    for key in details['recordingKeys']:
        indices[key] = idx
        idx += 1

    if indices.setdefault('distance', None) is None:
        return None
    elif indices.setdefault('clock', None) is None:
        return None

    splits = []
    element_idx = 0
    last_split = element_idx
    next_split = split_interval
    prev_time = 0 * UNITS.second
    prev_distance = 0
    for distance in details['recordingValues'][indices['distance']]:
        distance = distance * UNITS.kilometer
        if distance > next_split:
            cur_time = details['recordingValues'][indices['clock']][element_idx] * UNITS.second
            splits.append({'total_distance': next_split,
                           'split_distance': split_interval,
                           'total_time': cur_time,
                           'split_time': cur_time - prev_time
                           })
            prev_time = splits[-1]['total_time']
            prev_distance = splits[-1]['total_distance']
            next_split += split_interval
            last_split = element_idx

        element_idx += 1

    # Figure out last part of split
    if (last_split + 1) < len(details['recordingValues'][indices['distance']]):
        last_total_distance = (details['recordingValues'][indices['distance']][-1] * UNITS.kilometer).to(UNITS.mile)
        last_total_clock = details['recordingValues'][indices['clock']][-1] * UNITS.second
        splits.append({'total_distance': last_total_distance,
                       'split_distance': last_total_distance - prev_distance,
                       'total_time': last_total_clock,
                       'split_time': last_total_clock - prev_time})

    for split in splits:
        split['split_pace'] = split['split_time'] / split['split_distance']
        split['total_pace'] = split['total_time'] / split['total_distance']

    return splits


def default_title(run):
    # Otherwise some default
    return 'SmashRun Activity on %s' % (run['__localtime'])


def sr_get_coordinate(details):
    indices = {}
    idx = 0
    for key in details['recordingKeys']:
        indices[key] = idx
        idx += 1

    if indices.setdefault('latitude', None) is None:
        return None
    elif indices.setdefault('longitude', None) is None:
        return None
    else:
        # Just uses the last coordinate of the run
        lat = details['recordingValues'][indices['latitude']][-1]
        lng = details['recordingValues'][indices['longitude']][-1]
        return (lat, lng)


def sr_get_userinfo(smashrun):
    return smashrun.get_userinfo()


def sr_get_badges(smashrun):
    badges = []
    from_zone = dateutil.tz.tzutc()
    to_zone = dateutil.tz.tzlocal()

    for b in smashrun.get_badges():
        nofrag, frag = b['dateEarnedUTC'].split('.')
        utc = datetime.strptime(nofrag, '%Y-%m-%dT%H:%M:%S')
        utc = utc.replace(microsecond=int(frag))
        utc = utc.replace(tzinfo=from_zone)
        local = utc.astimezone(to_zone)
        badges.append((b, local))

    badges.reverse()
    return badges


def sr_get_badge_photos(activity_id, badges):
    photos = []
    for badge in badges:
        url = badge['image']
        dirname, filename = os.path.split(url)
        size_dir = os.path.basename(dirname)
        if size_dir == 'medium':
            size_dir = 'full'
        full_url = '/'.join([os.path.dirname(dirname), size_dir, filename])

        logging.info("Downloading full size image for %s" % (badge['name']))
        request = requests.get(full_url)
        tmpfile = tempfile.NamedTemporaryFile(prefix='dayonerun_%s_' % (activity_id), delete=False)
        if request.status_code == 200:
            tmpfile.write(request.content)
            tmpfile.close()
        else:
            logging.warning("Unable to download badge %s at %s. Trying normal size image." % (badge['name'], full_url))
            request = requests.get(url)
            if request.status_code != 200:
                logging.warning("Unable to download badge %s at %s" % (badge['name'], url))
                os.unlink(tmpfile)
                tmpfile = None
            else:
                tmpfile.write(request.content)
                tmpfile.close()
        if tmpfile is not None:
            photos.append(tmpfile.name)

    return photos


def sr_get_runs(smashrun, start, stop, userinfo, badges):
    from_zone = dateutil.tz.tzutc()
    to_zone = dateutil.tz.tzlocal()

    logging.info("Retriving SmashRuns START: %s" % (start))
    logging.info("                     STOP: %s" % (stop))

    activities = []
    # FIXME: If smashrun-client is fixed, can remove this buggy_start stuff
    delta = start - start.replace(tzinfo=from_zone)
    buggy_start = start - delta
    for r in smashrun.get_activities(since=buggy_start):
        # 2016-11-17T07:11:00-08:00
        dt = r['startDateTimeLocal'][:-6]
        tz = r['startDateTimeLocal'][-6:]
        offset = (int(tz[1:3]) * 60 * 60) + (int(tz[4:6]) * 60)
        if tz[0] == '-':
            offset = -offset

        local = datetime.strptime(dt, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=tzoffset(None, offset))
        if local > stop:
            logging.debug("Dropping activity on %s after stop date %s" % (local, stop))
        else:
            logging.info("Adding activity on %s" % (local))
            activities.append((r, local))
    activities.reverse()

    results = []
    for activity, localtime in activities:
        if activity['activityType'] != 'running':
            logging.debug("Dropping non-running activity (%s) on %s" % (activity['activityType'], localtime))
            continue

        logging.info("Adding %s from %s" % (activity['activityType'], localtime))
        details = smashrun.get_activity(activity['activityId'])
        logging.debug("SMASHRUN_ACTIVITY(%s)=%s" % (activity['activityId'], pprint.pformat(details)))
        splits = sr_get_split_info(details)
        activity['__id'] = {'smashrun': activity['activityId']}
        activity['__activity_urls'] = {'smashrun': 'http://smashrun.com/%s/run/%s' % (userinfo['userName'],
                                                                                      activity['activityId'])}
        activity['__title_fn'] = CFG_ACTIVITY_TITLE_FN
        activity['__notes'] = activity['notes'] + "\n"
        activity['__localtime'] = localtime
        activity['__tags'] = ['smashrun']
        activity['__details'] = details
        activity['__userinfo'] = userinfo
        activity['__splits'] = splits
        activity['__coordinate'] = sr_get_coordinate(details)
        activity['__badges'] = []
        min_badge_time = datetime.combine(localtime, datetime.min.time()).replace(tzinfo=to_zone)
        max_badge_time = datetime.combine(localtime, datetime.max.time()).replace(tzinfo=to_zone)
        for badge, badge_localtime in badges:
            if badge_localtime >= min_badge_time and badge_localtime <= max_badge_time:
                logging.info("Adding badge %s to run ID %s on %s" % (badge['name'], activity['activityId'], localtime))
                activity['__badges'].append(badge)

        activity['__photos'] = []
        activity['__photos'] = sr_get_badge_photos(activity['activityId'], activity['__badges'])

        results.append(activity)

    return results


def gen_split_markdown(splits):
    table = ''
    table += 'Distance | Total Time | Split Time | Split Pace | Total Pace\n'
    table += '-------- | ---------- | ---------- | ---------- | ----------\n'
    for split in splits:
        table += '%.2f | ' % (split['total_distance'].magnitude)
        table += '%s | ' % (time_string(split['total_time'].magnitude))
        table += '%s | ' % (time_string(split['split_time'].magnitude))
        table += '%s | ' % (time_string(split['split_pace'].magnitude))
        table += '%s\n' % (time_string(split['total_pace'].magnitude))
    table += '\n'
    return table


def create_journal_entry(args, run):
    split_markdown = gen_split_markdown(run['__splits'])
    dayone_args = ['dayone2']

    if args.journal is not None:
        dayone_args.extend(['--journal', args.journal])

    dayone_args.extend(['--date', run['__localtime'].strftime('%Y-%m-%d %H:%M:%S')])

    # Our tag + run-specific tags + command line tags
    dayone_args.extend(['--tags', 'dayonerun'] + run['__tags'] + args.tags)

    if not args.no_coordinates and run['__coordinate'] is not None:
        dayone_args.extend(['--coordinate', str(run['__coordinate'][0]), str(run['__coordinate'][1])])

    if len(run['__photos']) > 0:
        dayone_args.append('--photos')
        dayone_args.extend(run['__photos'])

    dayone_args.append('--')
    dayone_args.append('new')

    dayone_args = ["'%s'" % (x) if ' ' in x else x for x in dayone_args]
    logging.info("Invoking: %s" % (' '.join(dayone_args)))

    title_fn = run['__title_fn']
    if title_fn is None:
        title_fn = default_title
    entry_text = ''
    entry_text += '# %s\n' % (title_fn(run))
    entry_text += '# Notes\n%s\n' % (run['__notes'])
    entry_text += '# Splits\n%s\n' % split_markdown
    if len(run['__badges']) > 0:
        entry_text += '# Badges\n'
        for badge in run['__badges']:
            entry_text += '   * **%s**: %s\n' % (badge['name'], badge['requirement'])
        entry_text += '\n'
    entry_text += '# Misc\n'
    service_map = {'smashrun': 'SmashRun', 'strava': 'Strava'}

    for service, url in run['__activity_urls'].iteritems():
        ident = ''
        if service in run['__id']:
            ident = ' ID: `%s`' % (run['__id'][service])

        entry_text += '   * [%s Link](%s)%s\n' % (service_map[service], url, ident)

    if args.dryrun or args.debug:
        logging.info("Entry text:\n" + entry_text)
        if args.dryrun:
            return

    p = subprocess.Popen(dayone_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout, stderr = p.communicate(input=entry_text.encode('utf-8'))
    if stdout is None:
        stdout = ''
    if stderr is None:
        stderr = ''
    if p.returncode != 0:
        logging.error("Unable to create journal entry:")
        for line in stdout.splitlines() + stderr.splitlines():
            logging.error("    %s" % (line))
        raise RuntimeError("Unable to create journal entry with %s" % (' '.join(dayone_args)))
    else:
        logging.info("Created journal entry successfully")


def cleanup_runs(runs):
    for run in runs:
        for photo in run['__photos']:
            logging.info("Deleting temp photo %s" % (photo))
            os.unlink(photo)


def main(args):
    runs = []
    try:
        smashrun = smashrun_client(**args.credentials['smashrun'])
        userinfo = sr_get_userinfo(smashrun)
        badges = []
        if not args.no_badges:
            badges = sr_get_badges(smashrun)
        sr_runs = sr_get_runs(smashrun, args.start, args.stop, userinfo, badges)

        if not args.no_strava:
            strava = strava_client(**args.credentials['strava'])
            st_runs = st_get_runs(strava, args.start, args.stop)

        for run in sr_runs:
            if not args.no_strava:
                st_append_strava_info(strava, run, st_runs, args, args.credentials['google_maps_apikey'])
            create_journal_entry(args, run)

        if args.state_file or args.create_state_file:
            with open(args.state_file, 'w') as fh:
                fh.write("Command: %s\n" % (' '.join(sys.argv)))
                fh.write("LastUpdateStart: %s\n" % (args.start.strftime(STATE_FILE_TIME_FORMAT)))
                fh.write("%s%s\n" % (STATE_FILE_PREFIX, args.stop.strftime(STATE_FILE_TIME_FORMAT)))

    finally:
        cleanup_runs(runs)

    return 0

if __name__ == '__main__':
    sys.exit(main(setup(sys.argv[1:])))
