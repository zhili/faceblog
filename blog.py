#!/usr/bin/env python
#
# Copyright 2009 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import functools
import markdown
import os.path
import re
import tornado.web
import tornado.wsgi
import unicodedata
import wsgiref.handlers
import datetime
from google.appengine.api import users
from google.appengine.ext import db
from paging import *
import search
from google.appengine.api import memcache
from google.appengine.datastore import entity_pb
import logging

ARCHIVES_CACHE_TIME = 43200
CATEGORIES_REFRESH_TIME = 3600

class Entry(search.Searchable, db.Model):
    """A single blog entry."""
    author = db.UserProperty()
    title = db.StringProperty(required=True)
    slug = db.StringProperty(required=True)
    markdown = db.TextProperty(required=True)
    html = db.TextProperty(required=True)
    categories = db.ListProperty(db.Category)
    published = db.DateTimeProperty(auto_now_add=True)
    updated = db.DateTimeProperty(auto_now=True)
    INDEX_TITLE_FROM_PROP = 'slug'
    INDEX_ONLY = ['title', 'markdown']

class Comment(db.Model):
    # the comment associated with an entry.
    author = db.TextProperty(True)
    email = db.EmailProperty(True)
    url = db.LinkProperty(True)
    body = db.TextProperty(True)
    published = db.DateTimeProperty(auto_now_add=True)
    slug = db.StringProperty(required=True)
    
def administrator(method):
    """Decorate with this method to restrict to site admins."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if not self.current_user:
            if self.request.method == "GET":
                self.redirect(self.get_login_url())
                return
            raise tornado.web.HTTPError(403)
        elif not self.current_user.administrator:
            if self.request.method == "GET":
                self.redirect("/")
                return
            raise tornado.web.HTTPError(403)
        else:
            return method(self, *args, **kwargs)
    return wrapper


class BaseHandler(tornado.web.RequestHandler):
    """Implements Google Accounts authentication methods."""
    def get_current_user(self):
        user = users.get_current_user()
        if user: user.administrator = users.is_current_user_admin()
        return user

    def get_login_url(self):
        return users.create_login_url(self.request.uri)

    def render_string(self, template_name, **kwargs):
        # Let the templates access the users module to generate login URLs
        return tornado.web.RequestHandler.render_string(
            self, template_name, users=users, **kwargs)

    def get_archives(self, fullArchives=False):
        
        archives = memcache.get("recently_archives")
        if not archives:
            entries = db.Query(Entry).order('-published')
            markDict = {}
            archives = []
            for entry in entries:
                year = entry.published.year
                month = entry.published.month
                if (year, month) in markDict:
                    continue
                markDict[(year, month)] = 1
                archives.append((year, "%02d" % month, datetime.date(year, month, 1).strftime("%B %Y")))
                memcache.set("recently_archives", archives, ARCHIVES_CACHE_TIME)
        if not fullArchives and archives:
            return archives[:4]
        return archives
    
    def serialize_entities(self, models):
        # serialize using protocol buffer.
        if not models:
            return None
        elif isinstance(models, db.Model):
            return db.model_to_protobuf(models).Encode()
        else:
            return [db.model_to_protobuf(x).Encode() for x in models]

    def deserialize_entities(self, data):
        # deserialize from protocol buffer
        if not data:
            return None
        elif isinstance(data, str):
            return db.model_from_protobuf(entity_pb.EntityProto(data))
        else:
            return [db.model_from_protobuf(entity_pb.EntityProto(x)) for x in data]

PAGESIZE = 5

class HomeHandler(BaseHandler):
    def get(self):
        # entries = db.Query(Entry).order('-published').fetch(limit=5)
        self.redirect("/page/1/")
        return


class EntryHandler(BaseHandler):
    def get(self, slug):
        entry = db.Query(Entry).filter("slug =", slug).get()
        if not entry: raise tornado.web.HTTPError(404)
        nextEntry = db.Query(Entry).filter('published >', entry.published).order('published').get()
        prevEntry = db.Query(Entry).filter('published <', entry.published).order('-published').get()
        prevNextEntry = (prevEntry, nextEntry)
        comments = db.Query(Comment).filter("slug =", slug).fetch(1000)
        self.render("entry.html", entry=entry, comments=comments, archives=self.get_archives(), prevnextentry=prevNextEntry)

class PagingHandler(BaseHandler):
    def get(self, page):
        query = Entry.all().order('-published')
        thisPagedQuery = PagedQuery(query, PAGESIZE)
        pageNumber = int(page)
        entries = thisPagedQuery.fetch_page(pageNumber)
        if not entries:
            if not self.current_user or self.current_user.administrator:
                self.redirect("/compose")
                return

        pageInfo = [None, None]
        if thisPagedQuery.has_page(pageNumber+1):
            pageInfo[0] = pageNumber+1
        if thisPagedQuery.has_page(pageNumber-1):
            pageInfo[1] = pageNumber-1
        self.render("home.html", entries=entries, archives=self.get_archives(), pageinfo=pageInfo)


class ArchiveHandler(BaseHandler):
    def get(self):
        allCategories = memcache.get("categories")
        if not allCategories:
            entries = Entry.all()
            allCategories = set()
            for entry in entries:
                # logging.info(entry.categories)
                allCategories = allCategories.union(set(entry.categories))
            memcache.set("categories", allCategories, CATEGORIES_REFRESH_TIME)
        self.render("archive.html", categories = allCategories, archive_list=self.get_archives(fullArchives=True), archives=self.get_archives())

class MonthArchiveHandler(BaseHandler):
    def get(self, year, month):

        startDate = datetime.date(int(year), int(month), 1)
        endYear = int(year)
        endMonth = int(month)
        if startDate.month == 12:
            endYear += 1
            endMonth = 1
        else:
            endMonth += 1
        endDate = datetime.date(int(endYear), int(endMonth), 1)
        entries = Entry.all().filter('published >=', startDate).filter('published <', endDate).order('-published')
        self.render("archives.html", entries=entries, archives=self.get_archives())

class FeedHandler(BaseHandler):
    def get(self):
        entries = db.Query(Entry).order('-published').fetch(limit=10)
        self.set_header("Content-Type", "application/atom+xml")
        self.render("feed.xml", entries=entries)


class ComposeHandler(BaseHandler):
    @administrator
    def get(self):
        key = self.get_argument("key", None)
        entry = Entry.get(key) if key else None
        self.render("compose.html", entry=entry, archives=self.get_archives())

    @administrator
    def post(self):
        key = self.get_argument("key", None)
        if key:
            entry = Entry.get(key)
            entry.title = self.get_argument("title")
            entry.markdown = self.get_argument("markdown")
            categories = [c.strip() for c in self.get_argument("categories").split(',') if len(c.strip()) != 0]
            entry.categories = [cat if type(cat) == db.Category else db.Category(unicode(cat)) for cat in categories]
            entry.html = markdown.markdown(self.get_argument("markdown"))
        else:
            title = self.get_argument("title")
            slug = unicodedata.normalize("NFKD", title).encode(
                "ascii", "ignore")
            slug = re.sub(r"[^\w]+", " ", slug)
            slug = "-".join(slug.lower().strip().split())
            if not slug: slug = "entry"
            while True:
                existing = db.Query(Entry).filter("slug =", slug).get()
                if not existing or str(existing.key()) == key:
                    break
                slug += "-2"

            categories = [c.strip() for c in self.get_argument("categories").split(',') if len(c.strip()) != 0]
            standarlized_categories = [cat if type(cat) == db.Category else db.Category(unicode(cat)) for cat in categories]
            entry = Entry(
                author=self.current_user,
                title=title,
                slug=slug,
                markdown=self.get_argument("markdown"),
                html=markdown.markdown(self.get_argument("markdown")),
                categories=standarlized_categories
                )
        entry.put()
        # entry.index()
        entry.enqueue_indexing(url="/tasks/searchindexing")
        self.redirect("/entry/" + entry.slug)


class EntryModule(tornado.web.UIModule):
    def render(self, entry):
        return self.render_string("modules/entry.html", entry=entry)

class CommentModule(tornado.web.UIModule):
    def render(self, comment):
        return self.render_string("modules/comment.html", comment=comment)
    
class SearchHandler(BaseHandler):
    def get(self,):
        keyword = self.get_argument("s").strip()
        entries = Entry.search(keyword)
        self.render("search_result.html", entries=entries, archives=self.get_archives(), SearchKeyWord=keyword)

class SearchIndexingHandler(BaseHandler):
    """Handler for full text indexing task."""
    def post(self):
        key_str = self.get_argument('key')
        if key_str:
            key = db.Key(key_str)
            entity = db.get(key)
            if entity:
                entity.index()
            self.set_status(200)

class CategoriesHandler(BaseHandler):
    def get(self, category):
        # logging.info(category)
        entries = Entry.all().order("-published").filter("categories =", category.decode("utf-8"))
        self.render("categories.html", entries=entries, archives=self.get_archives())

class CommentHandler(BaseHandler):
    """handle comment posting.
    """
    def post(self):
        """ http post method.
        """
        author = self.get_argument("author")
        email = self.get_argument("email")
        url = self.get_argument("url")
        body = self.get_argument("body")
        slug = self.get_argument("slug")
        comment = Comment(
            author = author,
            email = email,
            url = url,
            body = body,
            slug = slug,
            )
        comment.put()
        self.redirect("/entry/" + comment.slug)
        
settings = {
    "blog_title": u"zhili/blog",
    "blog_subtitle": u"Random ideas & notes by zhilihu",
    "blog_about": u"I am a wireless engineer.",
    "template_path": os.path.join(os.path.dirname(__file__), "templates"),
    "ui_modules": {"Entry": EntryModule, "Comment": CommentModule},
    "xsrf_cookies": True,
}


application = tornado.wsgi.WSGIApplication([
    (r"/", HomeHandler),
    (r"/archive", ArchiveHandler),
    (r"/feed", FeedHandler),
    (r"/entry/([^/]+)", EntryHandler),
    (r"/compose", ComposeHandler),
    (r"/(\d{4})/(\d{2})", MonthArchiveHandler),
    (r"/page/(\d+)/", PagingHandler),
    (r"/search", SearchHandler),
    (r"/tasks/searchindexing", SearchIndexingHandler),
    (r"/categories/([^/]+)/", CategoriesHandler),
    (r"/postcomment", CommentHandler)
], **settings)


def main():
    wsgiref.handlers.CGIHandler().run(application)


if __name__ == "__main__":
    main()
