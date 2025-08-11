#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# A helper script to go through the latest comments added to a bugzilla
# to see if any of them link to external sites. If the reviewer deems them
# spammy, the script will tag them as such.
#
# Does not use any third party services -- the reviewer has to review every
# URL manually. Maybe using a sniffer service would be a good next step.
#
__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import requests
import argparse
import logging
import re
import shelve
import datetime
import tomllib
import os
import sys

from urllib.parse import urlparse

from typing import Optional, Set, Dict, List, Any

try:
    import jsonschema  # type: ignore
    has_jsonschema = True
except ImportError:
    has_jsonschema = False

VERSION = '2.0'

# Configuration schema for validation
CONFIG_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "main": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "pattern": r"^https?://[^\s/$.?#].[^\s]*$",
                    "description": "Bugzilla instance URL"
                },
                "apikey": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Bugzilla API key for authentication"
                },
                "logfile": {
                    "type": ["string", "null"],
                    "description": "Path to log file (optional)"
                },
                "cache": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Path to cache database file"
                }
            },
            "required": ["url", "apikey", "cache"],
            "additionalProperties": False
        },
        "spam": {
            "type": "object",
            "properties": {
                "comment_tag": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Tag to apply to spam comments"
                },
                "bug_group": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Group to move spam bugs to"
                },
                "bug_status": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Status to set for spam bugs"
                },
                "bug_resolution": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Resolution to set for spam bugs"
                }
            },
            "required": ["comment_tag", "bug_group", "bug_status", "bug_resolution"],
            "additionalProperties": False
        }
    },
    "required": ["main", "spam"],
    "additionalProperties": False
}

logger = logging.getLogger('bugjunker')

# Global variables for caching and state management
# These are initialized lazily to avoid unnecessary resource allocation

# Configuration data loaded from TOML file
# Contains Bugzilla URL, API key, cache paths, and other settings
config: Optional[Dict[str, Any]] = None

# Persistent HTTP session for Bugzilla API requests
# Reused across multiple requests to maintain connection pools and cookies
reqsession: Optional[requests.Session] = None

# Persistent shelve database for caching seen comments, attachments, and whitelists
# Stores: seencids, seenaids, okdomains, okfolks, lastrun timestamp
bzshelf: Optional[shelve.Shelf[Any]] = None


def validate_config_file_permissions(config_path: str) -> None:
    """
    Check that the configuration file has secure permissions.

    Warns if the config file is readable by others, as it contains
    sensitive information like API keys.

    Args:
        config_path: Path to the configuration file
    """
    try:
        stat_info = os.stat(config_path)
        # Check if file is readable by group or others (permissions & 0o044)
        if stat_info.st_mode & 0o044:
            logger.warning('Config file %s is readable by others - consider chmod 600', config_path)
    except OSError as e:
        logger.warning('Could not check permissions for %s: %s', config_path, e)


def validate_config_schema(config_data: Dict[str, Any]) -> None:
    """
    Validate configuration data against the defined schema.

    Uses jsonschema if available for comprehensive validation,
    otherwise falls back to basic manual validation of required fields.

    Args:
        config_data: The parsed configuration dictionary

    Raises:
        SystemExit: If configuration validation fails
    """
    if has_jsonschema:
        try:
            jsonschema.validate(config_data, CONFIG_SCHEMA)  # type: ignore
            logger.debug('Configuration schema validation passed')
        except jsonschema.ValidationError as e:  # type: ignore
            logger.error('Configuration validation error: %s', e.message)
            logger.error('Failed at path: %s', ' -> '.join(str(p) for p in e.absolute_path))
            if e.instance:
                logger.error('Invalid value: %s', e.instance)
            sys.exit(1)
        except jsonschema.SchemaError as e:  # type: ignore
            logger.error('Internal schema error: %s', e.message)
            sys.exit(1)


def validate_config_paths(config_data: Dict[str, Any]) -> None:
    """
    Validate that configured paths are accessible and writable where needed.

    Checks that cache directory is writable and logfile directory exists
    if a logfile is configured.

    Args:
        config_data: The parsed configuration dictionary
    """
    main_config = config_data.get('main', {})

    # Validate cache path
    cache_path = main_config.get('cache')
    if cache_path:
        cache_dir = os.path.dirname(cache_path)
        if cache_dir and not os.path.exists(cache_dir):
            try:
                os.makedirs(cache_dir, mode=0o700)
                logger.debug('Created cache directory: %s', cache_dir)
            except OSError as e:
                logger.error('Cannot create cache directory %s: %s', cache_dir, e)
                sys.exit(1)

        # Test if we can write to the cache location
        if cache_dir and not os.access(cache_dir, os.W_OK):
            logger.error('Cache directory %s is not writable', cache_dir)
            sys.exit(1)

    # Validate logfile path if specified
    logfile = main_config.get('logfile')
    if logfile:
        logfile_dir = os.path.dirname(logfile)
        if logfile_dir and not os.path.exists(logfile_dir):
            try:
                os.makedirs(logfile_dir, mode=0o755)
                logger.debug('Created log directory: %s', logfile_dir)
            except OSError as e:
                logger.error('Cannot create log directory %s: %s', logfile_dir, e)
                sys.exit(1)


def get_session() -> requests.Session:
    """
    Get or create a persistent HTTP session for Bugzilla API requests.

    This function implements a singleton pattern to reuse the same requests.Session
    object across multiple API calls, which improves performance by maintaining
    connection pools and cookies.

    Returns:
        requests.Session: A configured session object with appropriate User-Agent header

    Note:
        The session is lazily initialized on first call and cached in the global
        reqsession variable for subsequent calls.
    """
    global reqsession

    # Check if session already exists (singleton pattern)
    if reqsession is None:
        # Create new session instance
        reqsession = requests.session()  # type: ignore[assignment]

        # Set User-Agent header to identify our application to the server
        # This helps with debugging and allows server administrators to
        # identify traffic from this specific tool
        reqsession.headers.update({'User-Agent': f'bugjunker/{VERSION}'})

    return reqsession


def ban_hammer(spammers: Set[str]) -> None:
    """
    Ban identified spammer users in Bugzilla by disabling their accounts.

    This function disables email notifications and sets a login denial message
    for users who have been identified as spammers. Once banned, these users
    will not be able to log in and will see a "Spammer" message when attempting
    to access their account.

    Args:
        spammers: Set of user email addresses or usernames to ban

    Note:
        This action is irreversible through this script. Manual intervention
        by a Bugzilla administrator would be required to unban users.
    """
    params: Dict[str, Any] = dict()

    # Process each identified spammer
    for spammer in spammers:
        # Construct API path for user modification
        path = f'user/{spammer}'
        logger.info('Banning %s', spammer)

        # Prepare payload to disable the user account
        payload: Dict[str, Any] = {
            'email_enabled': False,         # Disable email notifications
            'login_denied_text': 'Spammer', # Set denial message shown on login attempts
        }

        # Execute the ban via Bugzilla REST API
        bz_put(path, params, payload)


def tag_hammer(spamcids: Set[int]) -> None:
    """
    Tag identified spam comments with a spam label in Bugzilla.

    This function applies a configurable spam tag to comments that have been
    identified as spam. The tag helps moderators and administrators quickly
    identify problematic content and can be used for filtering or reporting.

    Args:
        spamcids: Set of comment IDs to tag as spam

    Note:
        The spam tag is defined in the configuration file under 'spam.comment_tag'.
        Tagged comments remain visible but are marked for administrative review.
    """
    config = get_config()
    params: Dict[str, Any] = dict()

    # Process each spam comment ID (convert to set to remove duplicates)
    for cid in set(spamcids):
        logger.info('Tagging comment %s', cid)

        # Construct API path for comment tag modification
        path = f'bug/comment/{cid}/tags'

        # Prepare payload to add the spam tag
        payload: Dict[str, Any] = {
            'comment_id': cid,                      # Target comment ID
            'add': [config['spam']['comment_tag']], # Add the configured spam tag
        }

        # Execute the tagging via Bugzilla REST API
        bz_put(path, params, payload)


def bug_hammer(spambugs: Set[int]) -> None:
    """
    Mark entire bugs as spam by closing them and making them private.

    This function is used for the most severe spam cases, typically when the
    initial bug report (comment #0) itself is spam. It closes the bug with
    a spam-related status/resolution and moves it to a private group to hide
    it from public view.

    Args:
        spambugs: Set of bug IDs to mark as spam and hide

    Note:
        This is the most severe action - it effectively removes the entire bug
        from public visibility. The status, resolution, and group are configured
        in the configuration file under the 'spam' section.
    """
    config = get_config()
    params: Dict[str, Any] = dict()

    # Process each spam bug ID (convert to set to remove duplicates)
    for bugid in set(spambugs):
        logger.info('Junking bug %s', bugid)

        # Construct API path for bug modification
        path = 'bug/{bugid}'.format(bugid=bugid)

        # Prepare payload to close and hide the bug
        payload: Dict[str, Any] = {
            'groups': {'add': [config['spam']['bug_group']]}, # Move to private group (hide from public)
            'status': config['spam']['bug_status'],           # Set bug status (from config)
            'resolution': config['spam']['bug_resolution'],   # Set resolution (from config)
        }

        # Execute the bug modification via Bugzilla REST API
        bz_put(path, params, payload)


def bz_get(path: str, params: Dict[str, Any]) -> Any:
    """
    Perform a GET request to the Bugzilla REST API.

    This is a low-level utility function that handles authentication and
    constructs the full URL for Bugzilla API requests. It automatically
    adds the API key for authentication and returns the parsed JSON response.

    Args:
        path: The API endpoint path (relative to /rest/), e.g., 'bug/123' or 'user/user@example.com'
        params: Dictionary of query parameters to include in the request

    Returns:
        Any: The parsed JSON response from the Bugzilla API

    Note:
        The API key is automatically added from the configuration file.
        The base URL is constructed from config['main']['url'].
    """
    config = get_config()

    # Construct full URL by combining base URL with REST path
    url = f"{config['main']['url']}/rest/{path}"

    # Add API key to parameters for authentication
    params['api_key'] = config['main']['apikey']

    # Get the persistent HTTP session for connection reuse
    ses = get_session()

    # Execute the GET request
    res = ses.get(url, params=params)

    # Parse and return JSON response
    return res.json()


def bz_put(path: str, params: Dict[str, Any], payload: Dict[str, Any]) -> Any:
    """
    Perform a PUT request to the Bugzilla REST API to modify data.

    This is a low-level utility function that handles authentication and
    constructs the full URL for Bugzilla API modification requests. It
    automatically adds the API key for authentication and sends the payload
    as JSON data to update or modify Bugzilla entities.

    Args:
        path: The API endpoint path (relative to /rest/), e.g., 'bug/123' or 'user/user@example.com'
        params: Dictionary of query parameters to include in the request
        payload: Dictionary containing the data to be sent as JSON for modification

    Returns:
        Any: The parsed JSON response from the Bugzilla API

    Note:
        This function is used for all modification operations (user bans, comment tagging,
        bug status changes, etc.). The API key is automatically added from the configuration.
    """
    config = get_config()

    # Construct full URL by combining base URL with REST path
    url = f"{config['main']['url']}/rest/{path}"

    # Add API key to parameters for authentication
    params['api_key'] = config['main']['apikey']

    # Get the persistent HTTP session for connection reuse
    ses = get_session()

    # Execute the PUT request with JSON payload
    res = ses.put(url, params=params, json=payload)

    # Parse and return JSON response
    return res.json()


def get_shelf() -> shelve.Shelf[Any]:
    """
    Get or create a persistent shelve database to keep the state between runs.

    This function implements a singleton pattern to manage a persistent cache
    that stores information between script runs. The cache tracks processed
    comments/attachments, whitelisted domains/users, and the last run timestamp
    to avoid reprocessing the same data and maintain user decisions.

    Returns:
        shelve.Shelf[Any]: A persistent database object for storing cache data

    Cache Structure:
        - seencids: Set of comment IDs that have been processed
        - seenaids: Set of attachment IDs that have been checked
        - okdomains: Set of whitelisted domain names (approved by user)
        - okfolks: Set of whitelisted user emails/names (approved by user)
        - lastrun: Timestamp of last script execution (for incremental processing)

    Note:
        The cache file location is specified in config['main']['cache'].
        The singleton pattern ensures only one database connection per script run.
    """
    global bzshelf

    # Check if shelf is already initialized (singleton pattern)
    if bzshelf is not None:
        return bzshelf

    config = get_config()
    logger.info('Loading cache from %s', config['main']['cache'])

    # Open shelve database with 'c' flag (create if doesn't exist)
    bzshelf = shelve.open(config['main']['cache'], 'c')

    # Initialize cache structure with empty sets if keys don't exist

    # Track processed comment IDs to avoid duplicate processing
    if 'seencids' not in bzshelf:
        bzshelf['seencids'] = set()

    # Track checked attachment IDs to avoid duplicate attachment checks
    if 'seenaids' not in bzshelf:
        bzshelf['seenaids'] = set()

    # Store user-approved domain names to skip future prompts
    if 'okdomains' not in bzshelf:
        bzshelf['okdomains'] = set()

    # Store user-approved users to skip future prompts
    if 'okfolks' not in bzshelf:
        bzshelf['okfolks'] = set()

    # Track last run time for incremental processing (default to 24h lookback)
    if 'lastrun' not in bzshelf:
        bzshelf['lastrun'] = '24h'

    return bzshelf


def check_bad_urls(urls: list[str]) -> set[tuple[str, Optional[str]]]:
    """
    Check a list of URLs for potentially suspicious domains.

    This function filters URLs to identify those that may be spam by checking
    if their domains are in the whitelist of approved domains. URLs from
    non-whitelisted domains are flagged as potentially suspicious and will
    require user review.

    Args:
        urls: List of URL strings to check for suspicious domains

    Returns:
        set[tuple[str, Optional[str]]]: Set of tuples containing:
            - First element: The full suspicious URL
            - Second element: The domain name (netloc) or None if parsing failed

    Note:
        Only URLs with domains NOT in the okdomains whitelist are returned.
        Malformed URLs that can't be parsed are also flagged with domain=None.
    """
    badness: set[tuple[str, Optional[str]]] = set()
    bzs = get_shelf()

    # Check each URL against the whitelist
    for url in urls:
        try:
            # Parse the URL to extract domain information
            up = urlparse(url)

            # Check if domain is NOT in the approved domains whitelist
            if up.netloc not in bzs['okdomains']:
                # Flag this URL as potentially suspicious
                badness.add((url, up.netloc))

        except ValueError:
            # Handle malformed URLs that can't be parsed
            # Flag with None domain to indicate parsing failure
            badness.add((url, None))

    return badness


def is_junk_attachment(attid: int) -> bool:
    """
    Check if a bug attachment is likely to be spam and modify it if so.

    This function examines attachment metadata to determine if it's potentially
    spam. HTML attachments are considered highly suspicious and are automatically
    converted to safe plain text files. Legitimate attachments like patches and
    plain text files are preserved.

    Args:
        attid: The attachment ID to check

    Returns:
        bool: True if the attachment was identified and processed as junk,
              False if the attachment appears legitimate or doesn't exist

    Note:
        This function performs DESTRUCTIVE actions on HTML attachments by
        converting them to private plain text files named 'caution.txt'.
        Patches and plain text attachments are considered safe and preserved.
    """
    logger.info('    checking attachment %s', attid)

    # Construct API path for attachment retrieval
    path = f'bug/attachment/{attid}'

    # Fetch attachment metadata from Bugzilla
    attinfo = bz_get(path, {})

    # Check if attachment exists in the response
    if attid not in attinfo['attachments']:
        return False

    # Extract attachment metadata
    attdata = attinfo['attachments'][attid]

    # Allow legitimate attachment types to pass through
    if attdata['is_patch'] or attdata['content_type'] in ('text/plain',):
        # Patches and plain text are considered safe
        return False

    # Check for HTML attachments - almost always spam
    if attdata['content_type'] == 'text/html':
        # HTML attachments are highly suspicious - convert to safe format
        logger.info('    junking attachment %s', attid)

        # Prepare payload to neutralize the attachment
        payload: Dict[str, Any] = {
            'content_type': 'text/plain',  # Change to safe content type
            'filename': 'caution.txt',     # Replace with warning filename
            'is_private': True,            # Make it private to reduce exposure
        }

        # Execute the attachment modification
        bz_put(path, {}, payload)
        return True

    # Other content types are not currently processed
    return False


def process_bugs(bugs: List[Any]) -> tuple[set[str], set[int], set[int]]:
    """
    Process a list of bugs to identify spam comments, attachments, and users.

    This is the main spam detection engine that analyzes bug comments for
    suspicious URLs, checks attachments, and prompts the user for decisions
    about potentially spammy content. It maintains state to avoid reprocessing
    and respects user whitelisting decisions.

    Args:
        bugs: List of bug dictionaries from Bugzilla API containing bug metadata

    Returns:
        tuple[set[str], set[int], set[int]]: A 3-tuple containing:
            - spammers: Set of user emails/names identified as spammers
            - spamcids: Set of comment IDs that should be tagged as spam
            - spambugs: Set of bug IDs that should be completely hidden (spam bug reports)

    Interactive Behavior:
        For each suspicious URL found, the user is prompted with options:
        - 'b' (ban): Mark the user as spammer and tag content as spam
        - 'a' (allow): Allow this specific URL but don't whitelist the domain
        - 'w' (whitelist): Add domain and user to permanent whitelist

    Note:
        This function modifies the persistent cache to track processed items
        and user decisions. Comment #0 spam results in the entire bug being hidden.
    """
    # Initialize collections to track identified spam
    spammers: set[str] = set()      # Users identified as spammers
    spamcids: set[int] = set()      # Comment IDs to be tagged as spam
    spambugs: set[int] = set()      # Bug IDs to be completely hidden

    # Get persistent cache and configuration
    bzs = get_shelf()
    config = get_config()

    # Process each bug in the provided list
    for bug in bugs:
        logger.info('Analyzing [%s]: %s', bug['id'], bug['summary'])

        # Fetch all comments for this bug
        params: dict[str, Any] = dict()
        bugid = bug['id']
        path = f'bug/{bugid}/comment'
        comments = bz_get(path, params)

        # Process comments for each bug (nested structure from API)
        for _, f2 in comments['bugs'].items():
            c_count = -1  # Track comment position (0 = initial bug report)

            # Analyze each individual comment
            for comment in f2['comments']:
                c_count += 1
                cid = comment['id']

                # Skip comments we've already processed
                if cid in bzs['seencids']:
                    continue

                # Mark comment as seen (unless dry run)
                if not cmdargs.dry_run:
                    bzs['seencids'].add(cid)

                creator = comment['creator']

                # Skip comments from whitelisted users
                if creator in bzs['okfolks']:
                    continue

                tags = comment['tags']

                # Skip comments already tagged as spam
                if config['spam']['comment_tag'] in tags:
                    continue

                # Auto-tag comments from known spammers
                if creator in spammers:
                    if not cmdargs.dry_run:
                        spamcids.add(cid)
                    logger.info('    auto-tagging comment by %s: %s', creator, cid)
                    continue

                # Check attachments if enabled
                if cmdargs.checkatt:
                    attid = comment['attachment_id']
                    if attid is not None and attid not in bzs['seenaids']:
                        if not cmdargs.dry_run:
                            bzs['seenaids'].add(attid)
                            if is_junk_attachment(attid):
                                logger.info('    check if spammer: %s', creator)

                # Look for HTTP URLs in comment text and bug URL field
                if 'http' in bug['url'] or 'http' in comment['text']:
                    # Extract all HTTP/HTTPS URLs from comment text
                    urls = re.findall(r'(https?://\S+)', comment['text'])

                    # Include bug URL field if present
                    if len(bug['url']):
                        urls.append(bug['url'])

                    # Check URLs against whitelist
                    badness = check_bad_urls(urls)

                    # Process each suspicious URL found
                    for badurl, baddomain in badness:
                        # Skip if creator already identified as spammer
                        if creator in spammers:
                            logger.info('    spamcid: %s', cid)
                            spamcids.add(cid)
                            continue

                        # Present suspicious URL to user for decision
                        logger.info('    ---')
                        logger.info('    creator: %s', creator)
                        logger.info('    sus URL: %s', badurl)
                        logger.info('       link: %s/show_bug.cgi?id=%s#c%s',
                                    config['main']['url'], bugid, c_count)
                        baw = input('    (b)an, (a)llow, (w)hitelist %s: ' % baddomain)

                        # Handle user decision: Allow this URL
                        if baw == 'a':
                            if not cmdargs.dry_run:
                                bzs.sync()  # Save cache state
                            continue

                        # Handle user decision: Whitelist domain and user
                        if baw == 'w':
                            if not cmdargs.dry_run:
                                logger.info('    whitelisted %s', baddomain)
                                bzs['okdomains'].add(baddomain)  # Whitelist domain
                                bzs['okfolks'].add(creator)     # Whitelist user
                                bzs.sync()  # Save whitelist additions
                            continue

                        # Default action (or explicit ban): Mark as spam
                        logger.info('    spamcid: %s', cid)
                        spamcids.add(cid)
                        logger.info('    spammer: %s', creator)
                        spammers.add(creator)

                        # Special case: If comment #0 is spam, hide entire bug
                        if c_count == 0:
                            spambugs.add(bug['id'])

    return spammers, spamcids, spambugs


def get_config() -> Dict[str, Any]:
    """
    Load and return the configuration from the TOML file.

    This function implements a singleton pattern to load the configuration file
    only once per script execution. The configuration contains all settings
    needed for Bugzilla API access, spam detection parameters, and file paths.

    Returns:
        Dict[str, Any]: The parsed configuration dictionary containing:
            - main: Bugzilla connection settings (url, apikey, cache, logfile, spamtag)
            - spam: Spam handling settings (comment_tag, bug_group, bug_status, bug_resolution)

    Note:
        The configuration file path is taken from the command-line argument
        cmdargs.config. The file is expected to be in TOML format.

    Raises:
        SystemExit: If the config file is missing, malformed, contains invalid
                   TOML syntax, or fails validation.
    """
    global config

    # Check if configuration is already loaded (singleton pattern)
    if config is None:
        logger.info('Loading configuration file %s', cmdargs.config)

        # Check if config file exists
        if not os.path.exists(cmdargs.config):
            logger.error('Configuration file not found: %s', cmdargs.config)
            sys.exit(1)

        # Check file permissions for security
        validate_config_file_permissions(cmdargs.config)

        try:
            # Load and parse TOML configuration file
            # Open in binary mode as required by tomllib
            with open(cmdargs.config, 'rb') as f:
                config = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            logger.error('Error parsing TOML configuration file: %s', e)
            sys.exit(1)
        except OSError as e:
            logger.error('Error reading configuration file %s: %s', cmdargs.config, e)
            sys.exit(1)

        # Validate configuration schema
        validate_config_schema(config)

        # Validate paths and create directories if needed
        validate_config_paths(config)

    return config


def main() -> None:
    """
    Main entry point for the Bugzilla spam detection and removal tool.

    This function orchestrates the entire spam detection workflow:
    1. Sets up logging based on command-line arguments
    2. Loads configuration and initializes persistent cache
    3. Queries Bugzilla for recent changes since last run
    4. Processes bugs to identify spam content
    5. Executes appropriate actions (ban users, tag comments, hide bugs)
    6. Updates the last run timestamp for incremental processing

    The function supports dry-run mode for testing and uses incremental
    processing to avoid reprocessing the same bugs on subsequent runs.

    Note:
        This function relies on global variables for configuration and
        command-line arguments that are set up before main() is called.
    """
    global config
    global logger

    # Initialize logging with DEBUG level (will be filtered by handlers)
    logger.setLevel(logging.DEBUG)

    # Set up console logging handler
    sch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')  # Simple format for console
    sch.setFormatter(formatter)

    # Configure console logging level based on command-line flags
    if cmdargs.quiet:
        sch.setLevel(logging.CRITICAL)  # Only show critical errors
    elif cmdargs.debug:
        sch.setLevel(logging.DEBUG)     # Show all debug information
    else:
        sch.setLevel(logging.INFO)      # Default: show info and above

    logger.addHandler(sch)

    # Load configuration from TOML file
    config = get_config()

    # Set up file logging if configured
    if config['main']['logfile']:
        fch = logging.FileHandler(config['main']['logfile'])
        # More detailed format for file logs (includes process ID and timestamp)
        fmt = '[%(process)d] %(asctime)s - %(message)s'
        fch.setFormatter(logging.Formatter(fmt))
        fch.setLevel(logging.INFO)  # Always log INFO and above to file
        logger.addHandler(fch)

    # Initialize persistent cache for tracking state between runs
    bzs = get_shelf()

    # Determine time range for this run (incremental processing)
    if cmdargs.lookback is not None:
        # Use explicit lookback period from command line
        lastrun = cmdargs.lookback
    else:
        # Use cached timestamp from last run (or default '24h')
        lastrun = bzs['lastrun']

    # Prepare Bugzilla API query parameters for bug changes
    params: Dict[str, str] = {
        'chfieldfrom': lastrun,                                    # Changes since this time
        'include_fields': 'id,summary,last_change_time,url',      # Minimize data transfer
    }
    logger.info('Querying %s for changes since %s', config['main']['url'], lastrun)

    # Record current time for next run's timestamp
    unow = datetime.datetime.now(datetime.timezone.utc)

    # Query Bugzilla for bugs with changes since last run
    jdata = bz_get('bug', params)

    # Update last run timestamp (unless in dry-run mode)
    if not cmdargs.dry_run:
        bzs['lastrun'] = unow.strftime('%Y-%m-%d %H:%M:%S')

    # Process any bugs that were found
    if len(jdata['bugs']):
        # Run the main spam detection engine
        spammers, spamcids, spambugs = process_bugs(jdata['bugs'])

        # Handle dry-run mode
        if cmdargs.dry_run:
            logger.info('Dry run, not performing any actions')
            return

        # Execute spam removal actions if any spam was found
        if len(spammers) or len(spamcids) or len(spambugs):
            # Apply the three levels of spam countermeasures
            ban_hammer(spammers)    # Ban user accounts
            tag_hammer(spamcids)    # Tag spam comments
            bug_hammer(spambugs)    # Hide entire spam bug reports
        else:
            logger.info('No new spam found')
    else:
        logger.info('No changes since %s', lastrun)

    # Clean up: close the persistent cache database
    bzs.sync()
    bzs.close()


if __name__ == '__main__':
    description = ('Junk spammy bugzilla comments and ban their authors.')
    parser = argparse.ArgumentParser(description=description, prog='bz-comment-junker.py')
    parser.add_argument('-c', '--config', required=True,
                        help='Configuration file')
    parser.add_argument('-q', '--quiet', action='store_true', default=False,
                        help='Output only errors')
    parser.add_argument('-d', '--debug', action='store_true', default=False,
                        help='Add debugging info')
    parser.add_argument('-n', '--dry-run', action='store_true', default=False,
                        help='Dry run, do not perform any actions or store anything')
    parser.add_argument('-l', '--lookback', default=None,
                        help='How far back to look (default: since last run, or 24h if no cached data)')
    parser.add_argument('-a', '--check-attachments', action='store_true', dest='checkatt', default=False,
                        help='Check attachments for junkiness')

    cmdargs = parser.parse_args()
    main()
