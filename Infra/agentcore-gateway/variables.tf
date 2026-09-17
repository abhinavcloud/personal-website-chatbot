
locals {
  blog_tools = {
    list_blogs = {
      description = "List all blogs (metadata only: title, subtitle, date, readingTime, tags, icon). Call read_blog for the actual article body."
      properties  = {}
    }
    get_first_blog = {
      description = "Return the single oldest blog (metadata only)."
      properties  = {}
    }
    get_last_blog = {
      description = "Return the single most recent blog (metadata only)."
      properties  = {}
    }
    get_latest_blogs = {
      description = "Return the n most recent blogs, newest first (metadata only)."
      properties = {
        n = { type = "integer", required = false }
      }
    }
    get_oldest_blogs = {
      description = "Return the n oldest blogs, oldest first (metadata only)."
      properties = {
        n = { type = "integer", required = false }
      }
    }
    read_blog = {
      description = "Read the complete contents (full markdown body + metadata) of a single blog post by its path. The only tool that returns article text."
      properties = {
        path = { type = "string", required = true }
      }
    }
  }

  projects_tools = {
    list_projects = {
      description = "List all projects (metadata only: title, subtitle, date, readingTime, tags, icon). Call read_projects for the actual body."
      properties  = {}
    }
    get_first_project = {
      description = "Return the single oldest project (metadata only)."
      properties  = {}
    }
    get_last_project = {
      description = "Return the single most recent project (metadata only)."
      properties  = {}
    }
    get_latest_projects = {
      description = "Return the n most recent projects, newest first (metadata only)."
      properties = {
        n = { type = "integer", required = false }
      }
    }
    get_oldest_projects = {
      description = "Return the n oldest projects, oldest first (metadata only)."
      properties = {
        n = { type = "integer", required = false }
      }
    }
    read_projects = {
      description = "Read the complete contents (full markdown body + metadata) of a single project by its path. The only tool that returns project text."
      properties = {
        path = { type = "string", required = true }
      }
    }
  }

  resume_tools = {
    read_resume = {
      description = "Read Abhinav's resume: structured contact fields (name, title, location, phone, email, linkedin, github, website) plus the free-text body. Always use this for any contact-info question."
      properties  = {}
    }
  }
}

variable "project_name" { type = string}
variable "gateway_exec" { type = string}
variable "lambda_resume" {type = string}
variable "lambda_projects" {type = string}
variable "lambda_blog" {type = string}