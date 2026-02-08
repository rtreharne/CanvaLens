def staff_identity(request):
    user = getattr(request, "user", None)
    if user and user.is_authenticated and getattr(user, "is_staff", False):
        return {"staff_username": user.username}
    return {"staff_username": ""}
