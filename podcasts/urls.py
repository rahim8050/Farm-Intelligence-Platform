from django.urls import path

from podcasts import views

urlpatterns = [
    path(
        "podcasts/",
        views.PodcastListView.as_view(),
        name="podcasts-list",
    ),
    path(
        "podcasts/<str:podcast_id>/",
        views.PodcastDetailView.as_view(),
        name="podcasts-detail",
    ),
    path(
        "podcasts/<str:podcast_id>/episodes/",
        views.PodcastEpisodeListView.as_view(),
        name="podcasts-episodes",
    ),
    path(
        "podcasts/<str:podcast_id>/refresh/",
        views.PodcastRefreshView.as_view(),
        name="podcasts-refresh",
    ),
    path(
        "podcasts/episodes/<int:episode_id>/stream/",
        views.PodcastEpisodeStreamView.as_view(),
        name="podcasts-episode-stream",
    ),
]
