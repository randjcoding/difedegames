from contextlib import contextmanager

from .security import COOKIE_NAME, check_csrf, session_user


@contextmanager
def authenticated(request, mutation=False):
    with request.app.state.sessions.begin() as db:
        user, sess = session_user(db, request.cookies.get(COOKIE_NAME))
        if mutation:
            check_csrf(request, sess)
        yield db, user, sess
