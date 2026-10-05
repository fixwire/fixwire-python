from django.urls import path

from blog import views

urlpatterns = [
    path("", views.index),
    path("articles/<slug:slug>/", views.article),
    path("articles/<slug:slug>/like/", views.like),
    path("newsletter/", views.newsletter),
]
