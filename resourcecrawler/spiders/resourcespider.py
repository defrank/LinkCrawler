###############################################################################
# IMPORTS
###############################################################################

import os
import re
import mimetypes
import httplib2
from copy import deepcopy
from urlparse import urlparse
from scrapy import log
from scrapy.http import Request
from scrapy.selector import Selector
from scrapy.contrib.spiders import CrawlSpider, SitemapSpider, Rule
from scrapy.contrib.linkextractors import LinkExtractor
from twisted.internet.error import TimeoutError
from resourcecrawler.items import ResourceItem


###############################################################################
# GLOBALS
###############################################################################

MIME_EXTENSIONS = deepcopy(mimetypes.types_map)
MIME_EXTENSIONS.update(mimetypes.common_types)
MIME_TYPES = set(typ.strip() for typ in MIME_EXTENSIONS.itervalues())
CONTENT_TYPES = {}
for mime in MIME_TYPES:
    typ, subtyp = mime.split('/')
    current_types = CONTENT_TYPES.get(typ, None)
    if current_types is None:
        CONTENT_TYPES[typ] = set([subtyp])
    else:
        current_types.add(subtyp)
# Defaults.
DEFAULT_CONTENTTYPES = ['application/pdf']
#    Filepaths.
DESKTOP_PATH = os.path.expandvars('$HOME/Desktop')
DEFAULT_FILENAME = 'resources.csv'
DEFAULT_FILEPATH = os.path.join(DESKTOP_PATH, DEFAULT_FILENAME) if os.path.exists(DESKTOP_PATH) else DEFAULT_FILENAME


###############################################################################
# CLASSES
###############################################################################

class ResourceSpider(CrawlSpider, SitemapSpider):
    """Crawl, find, and count links of a specified MIME type.  MIME types are 
    determined by explicitly specifying them, selecting a group of MIME types, 
    selecting MIME type subtypes, or specifying file extensions from which the 
    MIME type can be determined.

    CrawlSpider has precedence over SitemapSpider in regards to inheritance.

    """

    name = 'resourcespider'
    sitemap_rules = [('', 'parse_link')]

    @staticmethod
    def get_baseurl(url):
        """Try to determine the base URL and base path of a given URL.

        Arguments:
            url -- string of URL

        Returns:
            tuple -- string base URL, string base path

        """
        urlp = urlparse(url)
        base = '%s://%s' % (urlp.scheme or 'http', urlp.netloc or urlp.hostname)
        return base, os.path.dirname(urlp.path)

    @staticmethod
    def bytes2human(num):
        """Convert bytes to human readable format."""
        if not isinstance(num, (int, float)):
            return num
        for unit in ('B', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB'):
            hr = '%.2f%s' % (num, unit)
            if abs(num) < 1024.0:
                break
            num /= 1024.0
        return hr

    def __init__(self, content_types=DEFAULT_CONTENTTYPES, optimize=True,
            output_file=DEFAULT_FILEPATH, follow=True, 
            follow_external=False, include_sitemap=False, **kwargs):
        """Constructor.

        Arguments:
            start_urls -- list of URLs as strings
            content_types -- list of MIME types, groups, subtypes, or file
                extensions
            follow -- boolean will allow/prevent spider from crawling links
            follow_external -- boolean will allow/prevent spider from crawling 
                links external to start URL domains
            include_sitemap -- boolean will allow/prevent using website sitemap to 
                help crawl entire site

        """
        # Scrapy passes arguments as strings, a script may have converted the 
        # values into string representation.
        kwargs.update(
                content_types=content_types,
                optimize=optimize,
                output_file=output_file,
                follow=follow, 
                follow_external=follow_external, 
                include_sitemap=include_sitemap)
        for key, val in kwargs.iteritems():
            if isinstance(val, basestring):
                try:
                    kwargs[key] = eval(val)
                except TypeError:
                    pass

        # Call inherited's super init due to _compile_rules() being called to 
        # early.  Called at the end of this init.
        #super(CrawlSpider, self).__init__(**kwargs)
        SitemapSpider.__init__(self, **kwargs)

        # Start URLs.
        urls = [urlparse(url.strip())
                    if any(url.startswith(p) for p in ['http://', 'https://']) 
                    else urlparse('http://%s' % url.strip())
                for url in self.start_urls]
        self.start_urls = set(url.geturl() for url in urls)

        # Crawling restrictions.
        self.follow_external = self.follow and self.follow_external
        self.include_sitemap = self.follow and self.include_sitemap
        # Allowed domains.
        self.allowed_domains = None if self.follow_external else set(re.sub(r'^www\.', '', url.netloc) for url in urls)

        # MIME Types.
        self.mimetypes = set()
        # Content-types.
        for ct in self.content_types:
            ct = ct.strip()
            if ct.startswith('.'):
                # Extension.
                self.insert_extension(ct)
            else:
                # MIME/content type.
                found = False
                for typ, subtypes in CONTENT_TYPES.iteritems():
                    group = ['%s/%s' % (typ, subtype) for subtype in subtypes]
                    if ct == typ:
                        found = True
                        self.mimetypes.update(group)
                    elif any(ct == subtype for subtype in subtypes):
                        found = True
                        self.mimetypes.add('%s/%s' % (typ, ct))
                    elif ct in group:
                        found = True
                        self.mimetypes.add(ct)
                if not found:
                    self.insert_extension('.' + ct)

        # Rules.
        self.rules = (
            # First: find resources.
            Rule(LinkExtractor(
                    allow=(),
                    allow_domains=self.allowed_domains,
                    unique=True),
                follow=self.follow,
                callback='parse_link'),
        )
        self._compile_rules()  # Required by CrawlSpider.__init__()

        # Other.
        self.cookies_seen = set()
        self.found = set()
        self.seen = set()
        self.parsed = set()
        self.requested = set()
        self.http_interface = httplib2.Http()

    def start_requests(self):
        """CrawlSpider's start_requests() should take precedence over 
        SitemapSpider.
        
        """
        return CrawlSpider.start_requests(self)

    def isallowed(self, url):
        """Return if a URL is constricted to the restraints of the start URLs."""
        return (self.follow_external or not self.allowed_domains 
                or any(d in url for d in self.allowed_domains))

    def insert_extension(self, ext):
        """Insert an extension's MIME type."""
        mimetype = MIME_EXTENSIONS.get(ext, None)
        if mimetype is not None:
            self.mimetypes.add(mimetype)
        else:
            raise ValueError('"%s" is not associated with a valid MIME/content type' % ext)

    def get_header_info(self, url):
        """Determine the mimetype of a given URL by performing a HEAD request.
        A tuple of the MIME type and URL are returned because the URL may have 
        been changed if it was not an absolute path.

        Arguments:
            url -- string of URL to retrieve MIME type
            base_url -- string base URL used to determine an absolute URL
            base_path -- string of relative path

        Returns:
            tuple -- string of MIME type, string of absolute URL

        """
        response = None
        mimetype = ''
        size = None
        # httplib2 has advantage over urllib2 in that a redirect maintains a 
        # HEAD request.
        interface = self.http_interface
        try:
            response, content = interface.request(url, method='HEAD')
        except httplib2.ServerNotFoundError as e:
            mimetype = None
        except Exception as e:
            mimetype = None

        if response is not None:
            mimetype = response.get('content-type', mimetype)
            size = response.get('content-length', size)

        return mimetype, size

    def parse(self, response):
        """Override CrawlSpider.parse() to specify if links found on start URLs 
        should be followed.
        
        """
        return self._parse_response(response, self.parse_start_url, 
                cb_kwargs={}, follow=self.follow)

    def parse_start_url(self, response):
        """Include a start URL domain's sitemap in crawling if allowed."""
        url = response.url
        urlp = urlparse(url)
        sitemap_url = '%s://%s/sitemap.xml' % (urlp.scheme or 'http', 
                urlp.netloc or urlp.hostname)

        # Include sitemap?
        if self.include_sitemap and sitemap_url not in self.seen:
            sitemap_requests = [Request(sitemap_url, callback=self._parse_sitemap)]
            self.seen.add(sitemap_url)
            response_requests = list(self.parse_link(response))
            return (item_or_request for item_or_request in sitemap_requests + response_requests)
        else:
            return self.parse_link(response)

    def parse_link(self, response):
        """Find all resources that coincide with what was specified and also 
        find all followable links.
        
        Arguments:
            response -- returned Request object

        Returns:
            generator -- yields Resource Items followed by followable Requests

        """
        url = response.url
        if url in self.parsed:
            return
        else:
            self.parsed.add(url)

        mimetype = response.headers['Content-Type']
        base_url, base_path = ResourceSpider.get_baseurl(url)

        # Extract links.
        if 'text/javascript' in mimetype:
            # Perhaps other cases as well, but I've seen JS text files be 
            # parsed when it contained HTML.
            resources = Selector()
        else:
            try:
                # Consider including background-image found in [style], however
                # only inline styles can be retrieved.  Don't forget to extract
                # 'url(' prefix and ')' suffix when implemented!
                resources = response.css('[href],[src]')
            except AttributeError as e:
                resources = Selector()
        # We only want to crawl normal stuff.  hrefs will be used to filter 
        # returned Requests.
        hrefs = resources.css('[href]::attr(href)').extract()
        resources = resources.css('[src]::attr(src)').extract()
        resources.extend(hrefs)

        requests = set()
        for link in resources:
            link = link.strip()
            linkp = urlparse(link)
            if linkp.scheme in ('mailto', 'tel') or link.startswith(('#', 'mailto:', 'tel:')):
                # URLs to ignore.
                continue
            elif not urlparse(link).netloc:
                # Fix a relative URL.
                link = base_url + os.path.join('/', base_path, link)
            elif not urlparse(link).scheme:
                # Fix URL scheme.
                link = 'http://%s' % link

            # Determine if examined this URL before.
            mimetype = size = None
            if link not in self.seen:
                self.seen.add(link)
                # Get URL header information: mimetype, size
                if self.optimize:
                    mimetype, encoding = mimetypes.guess_type(link)
                if mimetype is None:
                    mimetype, size = self.get_header_info(link)

            if mimetype:
                # Yield Items.
                if any(mt in mimetype for mt in self.mimetypes) and link not in self.found:
                    size = ResourceSpider.bytes2human(int(size) if size is not None else size)
                    count = len(self.found) + 1
                    if count == 1:
                        log.msg('%5s %-16s %-8s %-64s' % ('COUNT', 'MIMETYPE', 'SIZE', 'REFERRER'), 
                                level=log.INFO)
                    log.msg('%4d: %-16s %-8s %-64s' % (count, mimetype, size, link), 
                            level=log.INFO)
                    # MIME type format example: 'text/html; charset=utf-8'
                    self.found.add(link)
                    yield ResourceItem(url=link, mimetype=mimetype, size=size, referrer=url)
                # Build Requests.
                if self.follow and any(href.strip() in link for href in hrefs):
                    requests.add(link)

        # Yield Requests after having yielded Items.
        for link in requests:
            if link not in self.requested and self.isallowed(link):
                self.requested.add(link)
                yield Request(link, callback=self.parse_link)
