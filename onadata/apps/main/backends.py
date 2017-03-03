from django.contrib.auth.models import User
from django.contrib.auth.backends import ModelBackend as DjangoModelBackend

from rest_framework.authentication import TokenAuthentication


class ModelBackend(DjangoModelBackend):
    def authenticate(self, username=None, password=None):
        print('ModelBackend', 'authenticate')
        """Username is case insensitive."""
        try:
            user = User.objects.get(username__iexact=username)
            if user.check_password(password):
                return user
        except User.DoesNotExist:
            return None


class TokenBackend(DjangoModelBackend):
    def authenticate(self, request=None, **kwargs):

        try:
            user, token = TokenAuthentication().authenticate(request=request)
            return user
        except Exception as exp:
            print(exp)
            return None
