# Overview
This is a script to collect running activities from [SmashRun](http://smashrun.com) and [Strava](http://strava.com) and create (hopefully) nice looking [DayOne](http://dayoneapp.com) journal entries.

# Journal Entry Contents
   * Any Smashrun badges you received for the run
   * Any Strava photos you uploaded for the run
   * A route map based on the Strava run
   * A table of your splits, calculated from the Smashrun data
   * Clickable URLs to the Smashrun and Strava pages for the run

# Screenshot
![DayOneRunLog Screenshot](http://i.imgur.com/ZXqvu5D.png)

# Requirements
## DayOne
This only writes into [Day One](http://dayoneapp.com), so you'll need to have a copy of that on your Mac. It also uses the CLI, so make sure you have the latest version.

## Accounts
So you'll need a few accounts to make this work. 

   * Here's a shameless plug for my personal [SmashRun](http://smashrun.com/nall/invite) invitation page
      * You'll also need to request an API key. Mail [hi@smashrun.com](mailto:hi@smashrun.com) to get that going
   * You'll need a [Strava](http://www.strava.com) account as well
      * You'll also need to request an API key. To set that up, login to Strava and go to Settings -> My API Application
   * Finally, you'll need a [Google Maps API key](https://developers.google.com/maps/documentation/javascript/get-api-key) in order to create the route maps

## Python Modules
You'll also need to install some Python modules (I think these are all available via pip).

   * [stravalib](https://github.com/hozn/stravalib)
   * [smashrun-client](https://github.com/campbellr/smashrun-client)
   * [pint](https://github.com/hgrecco/pint)
   * [yaml](http://pyyaml.org/)

# Setting up credentials
The credentials are all stored in a YAML file passed to `dayonerun.pl` on the command line. Here's an example YAML file:

## Example credentials file
```
smashrun:
    client_id: XXX
    client_secret: XXX
    access_token: XXX
    refresh_token: XXX

strava:
     client_id: XXX
     client_secret: XXX
     access_token: XXX

google_maps_apikey: XXX
```

Details on how to obtain access tokens is left as an exerecise to the reader at this point.

# Running the script
Once you have your credentials setup, you may want to create a separate journal in Day One for importing. Do that before running the script the first time and take note of the journal's name. For examples below, I'll use the journal named `Running`.

## Default times
By default, dayonerun.py will try to load all runs in the past day. You can modify this behavior with the `--start` and `--days` options.

    dayonerun.py --journal Running  --credentials_file credentials --no_coordinates

Note the `--no_coordinates` above. This is currently required to workaround a bug in DayOne where longitudes starting with `-` generate an error. It can be removed when that bug is fixed.

## Grab data further back in time
To get 3 days worth of data starting from Oct 25, 2016 you can do this:

    dayonerun.py --journal Running --credentials_file credentials --no_coordinates --start 2016-10-25 --days 3

# Advanced options
If you open the script, there are a few options you can tweak. These are described below.

## START\_TIME\_THRESHOLD\_IN\_SECS (Default: 90)
Used matching SmashRun and Strava runs. Runs are consider to not match unless their the delta between their start times are under this threshold.

## DISTANCE\_THRESHOLD\_IN\_METERS (Default: 150)
Used matching SmashRun and Strava runs. Runs are consider to not match unless their the delta between their total distances are under this threshold.

## STRAVA\_PHOTO\_SIZE (Default: 1000)
The image size for Strava photo requests. This default basically gets you images around 1024x768 in resolution.
