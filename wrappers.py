# vim: ft=python expandtab softtabstop=0 tabstop=4 shiftwidth=4
import collections
import copy
import dateutil
import functools
import logging
import polyline
import pprint
import requests
import os
import smashrun.utils as sru
import stravalib
import tempfile
import urllib

from smashrun.client import Smashrun
from datetime import datetime
from dateutil.tz import tzoffset


UNITS = sru.UNITS


def download_url(url):
    r = requests.get(url)
    if r.status_code == 200:
        with tempfile.NamedTemporaryFile(prefix='dayonerun_photo_', delete=False) as fh:
            fh.write(r.content)
            return fh.name
    else:
        logging.warning("Unable to download %s: %s" % (url, r.text))
        return None


def strava_client(client_id=None, client_secret=None, refresh_token=None, access_token=None):
    client = stravalib.client.Client()
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


class ActivityWrapper(object):
    def __init__(self, service, title_fn, details):
        self.service = service
        self.title_fn = title_fn
        self.details = details
        self.linked_activities = []
        self._badges = []
        self._photos = []
        self._tags = [self.service.id]

    def all_badges(self):
        badges = copy.deepcopy(self._badges)
        for a in self.linked_activities:
            badges.extend(a.badges)
        return badges

    def all_photos(self):
        photos = copy.deepcopy(self._photos)
        for a in self.linked_activities:
            photos.extend(a.photos)
        return photos

    def all_tags(self):
        tags = copy.deepcopy(self._tags)
        for a in self.linked_activities:
            tags.extend(a.tags)
        return tags

    @property
    def tags(self):
        return self.all_tags()

    @property
    def badges(self):
        return self.all_badges()

    @property
    def photos(self):
        return self.all_photos()


class SmashrunActivity(ActivityWrapper):
    def __init__(self, service, title_fn, details):
        super(SmashrunActivity, self).__init__(service, title_fn, details)
        self._splits = None

    @property
    def id(self):
        return self.details['activityId']

    @property
    def notes(self):
        return self.details['notes'] + "\n"

    @property
    def start_coordinates(self):
        return sru.get_start_coordinates(self.details)

    @property
    def start(self):
        return sru.get_start_time(self.details)

    @property
    def distance(self):
        return sru.get_distance(self.details).to(UNITS.meters)

    @property
    def splits(self):
        return self.__splits()

    @property
    def polyline(self):
        return polyline.encode(sru.get_coordinates(self.details))

    def __splits(self, split_interval=1.0 * UNITS.mile):
        if self._splits is None:
            distances = sru.get_records(self.details, 'distance')
            if distances is None:
                return None

            clocks = sru.get_records(self.details, 'clock')
            if clocks is None:
                return None

            splits = []
            element_idx = 0
            last_split = element_idx
            next_split = split_interval
            prev_time = 0 * UNITS.second
            prev_distance = 0
            for distance in distances:
                distance = distance * UNITS.kilometer
                if distance > next_split:
                    cur_time = clocks[element_idx] * UNITS.second
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
            if (last_split + 1) < len(distances):
                last_total_distance = (distances[-1] * UNITS.kilometer).to(UNITS.mile)
                last_total_clock = clocks[-1] * UNITS.second
                splits.append({'total_distance': last_total_distance,
                               'split_distance': last_total_distance - prev_distance,
                               'total_time': last_total_clock,
                               'split_time': last_total_clock - prev_time})

            for split in splits:
                split['split_pace'] = split['split_time'] / split['split_distance']
                split['total_pace'] = split['total_time'] / split['total_distance']

            self._splits = splits

        return self._splits


class StravaActivity(ActivityWrapper):
    def __init__(self, service, title_fn, details):
        super(StravaActivity, self).__init__(service, title_fn, details)

    @property
    def id(self):
        return self.details['id']

    @property
    def polyline(self):
        mapinfo = self.details.setdefault('map', {})
        return mapinfo.setdefault('polyline', None)

    @property
    def distance(self):
        return float(self.details['distance']) * UNITS.meters

    @property
    def start(self):
        # FIXME: This shouldn't be tzlocal, but the local timezone at the place of activity
        utc = datetime.strptime(self.details['start_date'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dateutil.tz.tzutc())
        return utc.astimezone(dateutil.tz.tzlocal())


class ServiceWrapper(object):
    def __init__(self, client, name, service_id, google_apikey=None, config=None):
        if config is None:
            raise ValueError("No config passed to %s service" % (service_id))

        self.client = client
        self.name = name
        self.id = service_id
        self.google_apikey = google_apikey
        self.config = config
        self.activities = {}
        self.primary = False

    def cleanup(self):
        for activity in self.activities.values():
            for photo in activity.photos:
                logging.info("Deleting %s:%s temp photo %s" % (self.name, activity.id, photo))
                os.unlink(photo)

    def download(self, start, stop, badges=True, photos=True, routes=True, activity_types=['run']):
        self.activities = self.download_activities(start, stop, badges, photos, routes, activity_types)

        for activity in self.activities.values():
            if photos:
                activity._photos = self.photos_for_activity(activity)
            if badges:
                activity._badges = self.badges_for_activity(activity)
                for badge in activity.badges:
                    image = self.image_for_badge(badge)
                    if image is not None:
                        activity._photos.insert(0, image)
            if routes:
                # Only the primary service generates route images
                if self.primary:
                    image = self.route_image_for_activity(activity)
                    if image is not None:
                        # Insert in front of all photos to give this priority
                        activity._photos.insert(0, image)

    def match_activity(self, service, activity):
        matched_id = None

        for entry in self.config['matched_activities']:
            # Does this run exist in the manual map?
            if service.id in entry and entry[service.id] == activity.id:
                if self.id in entry:
                    assert matched_id is None, "Found duplicate entries for %s:%s in manual matching map" % (service.id, activity.id)
                    matched_id = entry[self.id]

        # We may have gotten here and not found a match. If so, we have to try manually
        if matched_id is None:
            for candidate in self.activities.values():
                max_len = max(len(self.id), len(service.id))
                time_delta = abs(activity.start - candidate.start).total_seconds()
                dist_delta = abs((activity.distance - candidate.distance).magnitude)
                logging.debug("%s%s: START=%s DIST=%s" % (service.id, ' ' * (max_len - len(service.id)), activity.start, activity.distance))
                logging.debug("%s%s: START=%s DIST=%s" % (self.id, ' ' * (max_len - len(self.id)), candidate.start, candidate.distance))
                logging.debug("%s  TIME (%ss) (max: %s)" % (' ' * max_len, time_delta, self.config['max_start_time_delta_in_secs']))
                logging.debug("%s  DIST (%sm) (max: %s)" % (' ' * max_len, dist_delta, self.config['max_distance_delta_in_meters']))

                if time_delta < self.config['max_start_time_delta_in_secs']:
                    if dist_delta < self.config['max_distance_delta_in_meters']:
                        matched_id = candidate.id
                        break
                    else:
                        logging.debug("Not matching %s and %s: distance %s is greater than threshold %s" % (activity.id,
                                                                                                            candidate.id,
                                                                                                            dist_delta,
                                                                                                            self.config['max_distance_delta_in_meters']))
                else:
                    logging.debug("Not matching %s and %s: start time %s is greater than threshold %s" % (activity.id,
                                                                                                          candidate.id,
                                                                                                          time_delta,
                                                                                                          self.config['max_start_time_delta_in_secs']))
        return matched_id

    def merge_service(self, service):
        for activity in service.activities.values():
            matched_id = self.match_activity(service, activity)
            if matched_id is None:
                logging.warning("No %s activity found for %s:%s. Nothing to merge." % (self.id, service.id, activity.id))

            if matched_id is not None:
                logging.info("Merging %s:%s into %s:%s..." % (service.id, activity.id, self.id, matched_id))
                self.activities[matched_id].linked_activities.append(activity)

    def route_image_for_activity(self, activity):
        if activity.polyline is not None and self.google_apikey is not None:
            poly = urllib.quote(activity.polyline)
            url = 'https://maps.googleapis.com/maps/api/staticmap?size=640x640&path=weight:6%%7Ccolor:blue%%7Cenc:%s&key=%s' % (poly, self.google_apikey)  # noqa
            fname = download_url(url)
            if fname is not None:
                return fname

        return None

    def download_activities(self, start, stop, badges, photos, routes, activity_types):
        raise NotImplementedError("Subclass %s must implement download_activities" % (self.id))

    def url_for_activity(self, activity):
        raise NotImplementedError("Subclass %s must implement url_for_activity" % (self.id))

    def photos_for_activity(self, activity):
        raise NotImplementedError("Subclass %s must implement photos_for_activity" % (self.id))

    def badges_for_activity(self, activity):
        raise NotImplementedError("Subclass %s must implement badges_for_activity" % (self.id))

    def image_for_badge(self, badge):
        raise NotImplementedError("Subclass %s must implement image_for_badge" % (self.id))


class SmashrunWrapper(ServiceWrapper):
    def __init__(self, client, **kwargs):
        super(SmashrunWrapper, self).__init__(client, 'Smashrun', 'smashrun', **kwargs)
        self.badges = None
        self.badge_earned_info = None
        self.userinfo = client.get_userinfo()

    def url_for_activity(self, activity):
        return 'http://smashrun.com/%s/run/%s' % (self.userinfo['userName'], activity.id)

    def photos_for_activity(self, activity):
        # Smashrun doesn't support photos
        return []

    def badges_for_activity(self, activity):
        if self.badges is None:
            self.badges = []
            for b in self.client.get_badges():
                self.badges.append(b)
        # FIXME
        # activity['__photos'] = []
        # activity['__photos'] = sr_get_badge_photos(activity['activityId'], activity['__badges'])

        return []

    def image_for_badge(self, badge):
        url = badge['image']
        dirname, filename = os.path.split(url)
        size_dir = os.path.basename(dirname)
        if size_dir == 'medium':
            size_dir = 'full'
        full_url = '/'.join([os.path.dirname(dirname), size_dir, filename])

        logging.info("Downloading full size image for %s" % (badge['name']))
        fname = download_url(url)
        return fname

    def download_activities(self, start, stop, badges, photos, routes, activity_types):
        activity_type_map = {'running': 'run'}

        logging.info("Retriving SmashRuns START: %s" % (start))
        logging.info("                     STOP: %s" % (stop))

        # FIXME: Look at briefs first to filter on stop once smashrun-client supports it
        activities = []
        for r in self.client.get_activities(since=start):
            start_time = sru.get_start_time(r)
            if start_time > stop:
                logging.debug("Dropping activity on %s after stop date %s" % (start_time, stop))
            else:
                atype = r['activityType']
                btype = activity_type_map.setdefault(atype, None)
                if btype is None:
                    logging.warning("Found unknown smashrun activity '%s'. Ignoring" % (atype))
                elif btype in activity_types:
                    details = self.client.get_activity(r['activityId'])
                    logging.debug("SMASHRUN_ACTIVITY(%s)=%s" % (details['activityId'], pprint.pformat(details)))
                    activities.append(SmashrunActivity(self, self.config['activity_title_fn'], details))
                else:
                    logging.info("Dropping activity type '%s' for ID=%s on %s'" % (atype, r['activityId'], start_time))

        logging.info("Downloaded %d activities from Smashrun" % (len(activities)))

        # Store sorted oldest to newest
        result = collections.OrderedDict()
        for activity in sorted(activities, key=lambda x: sru.get_start_time(x.details)):
            result[activity.id] = activity

        return result


class StravaWrapper(ServiceWrapper):
    def __init__(self, client, **kwargs):
        super(StravaWrapper, self).__init__(client, 'Strava', 'strava', **kwargs)

    def download_activities(self, start, stop, badges, photos, routes, activity_types):
        activity_type_map = {'running': 'run'}

        logging.info("Retriving Strava Runs START: %s" % (start))
        logging.info("                       STOP: %s" % (stop))

        activities = []
        for activity in self.client.get_activities(after=start, before=stop):
            details = self.client.protocol.get('/activities/{id}', id=activity.id, include_all_efforts=True)
            activities.append(StravaActivity(self, self.config['activity_title_fn'], details))

        # Store sorted oldest to newest
        result = collections.OrderedDict()
        for activity in activities: # sorted(activities, key=lambda x: sru.get_start_time(x.details)):
            result[activity.id] = activity

        return result

    def url_for_activity(self, activity):
        return 'https://www.strava.com/activities/%s' % (activity.id)

    def photos_for_activity(self, activity):
        logging.info("Getting any photos for %s" % (activity.id))

        # WORKAROUND until stravalib.get_activity_photos is fixed to include photo_sources
        result_fetcher = functools.partial(self.client.protocol.get,
                                           '/activities/{id}/photos',
                                           id=activity.id, photo_sources=True, size=self.config['strava_photo_size'])

        photo_iterator = stravalib.client.BatchedResultsIterator(entity=stravalib.model.ActivityPhoto,
                                                                 bind_client=self.client,
                                                                 result_fetcher=result_fetcher)

        files = []
        for photo in photo_iterator:
            logging.debug("%s PHOTO: %s" % (self.id, pprint.pformat(photo)))
            logging.debug("%s         ref : %s" % (self.id, photo.ref))
            logging.debug("%s         urls: %s" % (self.id, pprint.pformat(photo.urls)))
            fname = download_url(photo.urls[str(self.config['strava_photo_size'])])
            if fname is not None:
                files.append(fname)
        return files

    def badges_for_activity(self, activity):
        # Strava doesn't have badges
        return []

