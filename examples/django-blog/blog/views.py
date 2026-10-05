import logging
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

import fixwire

log = logging.getLogger("blog")

ARTICLES: dict[str, dict[str, Any]] = {
    "hello-fixwire": {"title": "Hello, Fixwire", "likes": 3},
    "draft": {"title": "Unfinished", "likes": None},  # a data bug waiting to happen
}


def index(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"articles": sorted(ARTICLES)})


def article(request: HttpRequest, slug: str) -> JsonResponse:
    if slug not in ARTICLES:
        raise Http404("no such article")  # 404s are not error reports
    fixwire.set_tag("article", slug)
    return JsonResponse(ARTICLES[slug])


@csrf_exempt
def like(request: HttpRequest, slug: str) -> JsonResponse:
    a: dict[str, Any] = ARTICLES.get(slug) or {}
    fixwire.set_tag("article", slug)
    fixwire.add_breadcrumb(category="blog", message="like clicked", data={"slug": slug})
    # TypeError for drafts (likes is None): reported as unhandled, with the
    # request and the URL pattern /articles/<slug:slug>/like/ as transaction.
    a["likes"] = a["likes"] + 1
    return JsonResponse({"likes": a["likes"]})


@csrf_exempt
def newsletter(request: HttpRequest) -> HttpResponse:
    email = request.GET.get("email", "")
    try:
        raise ConnectionError("mail provider rejected %s" % email)
    except ConnectionError:
        # A handled failure: logged, which makes it an event. The email in
        # the message is masked before it leaves the server.
        log.exception("newsletter signup failed")
    return HttpResponse("We'll retry soon.", status=202)
