# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import os

from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError

from django.db import models
from django.core.validators import MinValueValidator

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from InvenTree.models import InvenTreeTree
from company.models import Company


class PartCategory(InvenTreeTree):
    """ PartCategory provides hierarchical organization of Part objects.
    """

    def get_absolute_url(self):
        return '/part/category/{id}/'.format(id=self.id)

    class Meta:
        verbose_name = "Part Category"
        verbose_name_plural = "Part Categories"

    @property
    def partcount(self):
        """ Return the total part count under this category
        (including children of child categories)
        """

        count = self.parts.count()

        for child in self.children.all():
            count += child.partcount

        return count

    @property
    def has_parts(self):
        return self.parts.count() > 0


@receiver(pre_delete, sender=PartCategory, dispatch_uid='partcategory_delete_log')
def before_delete_part_category(sender, instance, using, **kwargs):

    # Update each part in this category to point to the parent category
    for part in instance.parts.all():
        part.category = instance.parent
        part.save()

    # Update each child category
    for child in instance.children.all():
        child.parent = instance.parent
        child.save()


# Function to automatically rename a part image on upload
# Format: part_pk.<img>
def rename_part_image(instance, filename):
    base = 'part_images'

    if filename.count('.') > 0:
        ext = filename.split('.')[-1]
    else:
        ext = ''

    fn = 'part_{pk}_img'.format(pk=instance.pk)

    if ext:
        fn += '.' + ext

    return os.path.join(base, fn)


class Part(models.Model):
    """ Represents an abstract part
    Parts can be "stocked" in multiple warehouses,
    and can be combined to form other parts
    """

    def get_absolute_url(self):
        return '/part/{id}/'.format(id=self.id)

    # Short name of the part
    name = models.CharField(max_length=100, unique=True, help_text='Part name (must be unique)')

    # Longer description of the part (optional)
    description = models.CharField(max_length=250, help_text='Part description')

    # Internal Part Number (optional)
    # Potentially multiple parts map to the same internal IPN (variants?)
    # So this does not have to be unique
    IPN = models.CharField(max_length=100, blank=True, help_text='Internal Part Number')

    # Provide a URL for an external link
    URL = models.URLField(blank=True, help_text='Link to extenal URL')

    # Part category - all parts must be assigned to a category
    category = models.ForeignKey(PartCategory, related_name='parts',
                                 null=True, blank=True,
                                 on_delete=models.DO_NOTHING,
                                 help_text='Part category')

    image = models.ImageField(upload_to=rename_part_image, max_length=255, null=True, blank=True)

    default_location = models.ForeignKey('stock.StockLocation', on_delete=models.SET_NULL,
                                         blank=True, null=True,
                                         help_text='Where is this item normally stored?',
                                         related_name='default_parts')

    # Default supplier part
    default_supplier = models.ForeignKey('part.SupplierPart',
                                         on_delete=models.SET_NULL,
                                         blank=True, null=True,
                                         help_text='Default supplier part',
                                         related_name='default_parts')

    # Minimum "allowed" stock level
    minimum_stock = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)], help_text='Minimum allowed stock level')

    # Units of quantity for this part. Default is "pcs"
    units = models.CharField(max_length=20, default="pcs", blank=True)

    # Can this part be built from other parts?
    buildable = models.BooleanField(default=False, help_text='Can this part be built from other parts?')

    # Can this part be used to make other parts?
    consumable = models.BooleanField(default=True, help_text='Can this part be used to build other parts?')

    # Is this part "trackable"?
    # Trackable parts can have unique instances
    # which are assigned serial numbers (or batch numbers)
    # and can have their movements tracked
    trackable = models.BooleanField(default=False, help_text='Does this part have tracking for unique items?')

    # Is this part "purchaseable"?
    purchaseable = models.BooleanField(default=True, help_text='Can this part be purchased from external suppliers?')

    # Can this part be sold to customers?
    salable = models.BooleanField(default=False, help_text="Can this part be sold to customers?")

    notes = models.TextField(blank=True)

    def __str__(self):
        return "{n} - {d}".format(n=self.name, d=self.description)

    class Meta:
        verbose_name = "Part"
        verbose_name_plural = "Parts"

    @property
    def category_path(self):
        if self.category:
            return self.category.pathstring
        return ''

    @property
    def available_stock(self):
        """
        Return the total available stock.
        This subtracts stock which is already allocated
        """

        total = self.total_stock

        total -= self.allocation_count

        return max(total, 0)

    @property
    def can_build(self):
        """ Return the number of units that can be build with available stock
        """

        # If this part does NOT have a BOM, result is simply the currently available stock
        if not self.has_bom:
            return self.available_stock

        total = None

        # Calculate the minimum number of parts that can be built using each sub-part
        for item in self.bom_items.all():
            stock = item.sub_part.available_stock
            n = int(1.0 * stock / item.quantity)

            if total is None or n < total:
                total = n

        return max(total, 0)

    @property
    def active_builds(self):
        """ Return a list of outstanding builds.
        Builds marked as 'complete' or 'cancelled' are ignored
        """

        return [b for b in self.builds.all() if b.is_active]

    @property
    def inactive_builds(self):
        """ Return a list of inactive builds
        """

        return [b for b in self.builds.all() if not b.is_active]

    @property
    def quantity_being_built(self):
        """ Return the current number of parts currently being built
        """

        return sum([b.quantity for b in self.active_builds])

    @property
    def build_allocation(self):
        """ Return list of builds to which this part is allocated
        """

        builds = []

        for item in self.used_in.all():

            for build in item.part.active_builds:
                b = {}

                b['build'] = build
                b['quantity'] = item.quantity * build.quantity

                builds.append(b)

        return builds

    @property
    def allocated_build_count(self):
        """ Return the total number of this that are allocated for builds
        """

        return sum([a['quantity'] for a in self.build_allocation])

    @property
    def allocation_count(self):
        """ Return true if any of this part is allocated
        - To another build
        - To a customer order
        """

        return sum([
            self.allocated_build_count,
        ])

    @property
    def stock_entries(self):
        return [loc for loc in self.locations.all() if loc.in_stock]

    @property
    def total_stock(self):
        """ Return the total stock quantity for this part.
        Part may be stored in multiple locations
        """

        return sum([loc.quantity for loc in self.stock_entries])

    @property
    def has_bom(self):
        return self.bom_count > 0

    @property
    def bom_count(self):
        return self.bom_items.count()

    @property
    def used_in_count(self):
        return self.used_in.count()

    @property
    def supplier_count(self):
        # Return the number of supplier parts available for this part
        return self.supplier_parts.count()

    def export_bom(self, **kwargs):

        # Construct the export data
        header = []
        header.append('Part')
        header.append('Description')
        header.append('Quantity')
        header.append('Note')

        rows = []

        for it in self.bom_items.all():
            line = []

            line.append(it.sub_part.name)
            line.append(it.sub_part.description)
            line.append(it.quantity)
            line.append(it.note)

            rows.append([str(x) for x in line])

        file_format = kwargs.get('format', 'csv').lower()

        kwargs['header'] = header
        kwargs['rows'] = rows

        if file_format == 'csv':
            return self.export_bom_csv(**kwargs)
        elif file_format in ['xls', 'xlsx']:
            return self.export_bom_xls(**kwargs)
        elif file_format == 'xml':
            return self.export_bom_xml(**kwargs)
        elif file_format in ['htm', 'html']:
            return self.export_bom_htm(**kwargs)
        elif file_format == 'pdf':
            return self.export_bom_pdf(**kwargs)
        else:
            return None

    def export_bom_csv(self, **kwargs):

        # Construct header line
        header = kwargs.get('header')
        rows = kwargs.get('rows')

        # TODO - Choice of formatters goes here?
        out = ','.join(header)

        for row in rows:
            out += '\n'
            out += ','.join(row)

        return out

    def export_bom_xls(self, **kwargs):

        return ''

    def export_bom_xml(self, **kwargs):
        return ''

    def export_bom_htm(self, **kwargs):
        return ''

    def export_bom_pdf(self, **kwargs):
        return ''

    """
    @property
    def projects(self):
        " Return a list of unique projects that this part is associated with.
        A part may be used in zero or more projects.
        "

        project_ids = set()
        project_parts = self.projectpart_set.all()

        projects = []

        for pp in project_parts:
            if pp.project.id not in project_ids:
                project_ids.add(pp.project.id)
                projects.append(pp.project)

        return projects
    """


def attach_file(instance, filename):

    base = 'part_files'

    # TODO - For a new PartAttachment object, PK is NULL!!

    # Prefix the attachment ID to the filename
    fn = "{id}_{fn}".format(id=instance.pk, fn=filename)

    return os.path.join(base, fn)


class PartAttachment(models.Model):
    """ A PartAttachment links a file to a part
    Parts can have multiple files such as datasheets, etc
    """

    part = models.ForeignKey(Part, on_delete=models.CASCADE,
                             related_name='attachments')

    attachment = models.FileField(upload_to=attach_file, null=True, blank=True)


class BomItem(models.Model):
    """ A BomItem links a part to its component items.
    A part can have a BOM (bill of materials) which defines
    which parts are required (and in what quatity) to make it
    """

    def get_absolute_url(self):
        return '/part/bom/{id}/'.format(id=self.id)

    # A link to the parent part
    # Each part will get a reverse lookup field 'bom_items'
    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name='bom_items',
                             limit_choices_to={'buildable': True})

    # A link to the child item (sub-part)
    # Each part will get a reverse lookup field 'used_in'
    sub_part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name='used_in',
                                 limit_choices_to={'consumable': True})

    # Quantity required
    quantity = models.PositiveIntegerField(default=1, validators=[MinValueValidator(0)])

    # Note attached to this BOM line item
    note = models.CharField(max_length=100, blank=True, help_text='Item notes')

    def clean(self):

        # A part cannot refer to itself in its BOM
        if self.part == self.sub_part:
            raise ValidationError(_('A part cannot contain itself as a BOM item'))

        for item in self.sub_part.bom_items.all():
            if self.part == item.sub_part:
                raise ValidationError(_("Part '{p1}' is  used in BOM for '{p2}' (recursive)".format(p1=str(self.part), p2=str(self.sub_part))))

    class Meta:
        verbose_name = "BOM Item"

        # Prevent duplication of parent/child rows
        unique_together = ('part', 'sub_part')

    def __str__(self):
        return "{par} -> {child} ({n})".format(
            par=self.part.name,
            child=self.sub_part.name,
            n=self.quantity)


class SupplierPart(models.Model):
    """ Represents a unique part as provided by a Supplier
    Each SupplierPart is identified by a MPN (Manufacturer Part Number)
    Each SupplierPart is also linked to a Part object
    - A Part may be available from multiple suppliers
    """

    def get_absolute_url(self):
        return "/supplier-part/{id}/".format(id=self.id)

    class Meta:
        unique_together = ('part', 'supplier', 'SKU')

    # Link to an actual part
# The part will have a field 'supplier_parts' which links to the supplier part options
    part = models.ForeignKey(Part, on_delete=models.CASCADE,
                             related_name='supplier_parts')

    supplier = models.ForeignKey(Company, on_delete=models.CASCADE,
                                 related_name='parts')

    SKU = models.CharField(max_length=100, help_text='Supplier stock keeping unit')

    manufacturer = models.CharField(max_length=100, blank=True, help_text='Manufacturer')

    MPN = models.CharField(max_length=100, blank=True, help_text='Manufacturer part number')

    URL = models.URLField(blank=True)

    description = models.CharField(max_length=250, blank=True)

    # Default price for a single unit
    single_price = models.DecimalField(max_digits=10, decimal_places=3, default=0)

    # Base charge added to order independent of quantity e.g. "Reeling Fee"
    base_cost = models.DecimalField(max_digits=10, decimal_places=3, default=0)

    # packaging that the part is supplied in, e.g. "Reel"
    packaging = models.CharField(max_length=50, blank=True)

    # multiple that the part is provided in
    multiple = models.PositiveIntegerField(default=1, validators=[MinValueValidator(0)])

    # Mimumum number required to order
    minimum = models.PositiveIntegerField(default=1, validators=[MinValueValidator(0)])

    # lead time for parts that cannot be delivered immediately
    lead_time = models.DurationField(blank=True, null=True)

    @property
    def manufacturer_string(self):

        items = []

        if self.manufacturer:
            items.append(self.manufacturer)
        if self.MPN:
            items.append(self.MPN)

        return ' | '.join(items)

    def __str__(self):
        return "{sku} - {supplier}".format(
            sku=self.SKU,
            supplier=self.supplier.name)


class SupplierPriceBreak(models.Model):
    """ Represents a quantity price break for a SupplierPart
    - Suppliers can offer discounts at larger quantities
    - SupplierPart(s) may have zero-or-more associated SupplierPriceBreak(s)
    """

    part = models.ForeignKey(SupplierPart, on_delete=models.CASCADE, related_name='price_breaks')
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(0)])
    cost = models.DecimalField(max_digits=10, decimal_places=3)

    class Meta:
        unique_together = ("part", "quantity")

    def __str__(self):
        return "{mpn} - {cost}{currency} @ {quan}".format(
            mpn=self.part.MPN,
            cost=self.cost,
            currency=self.currency if self.currency else '',
            quan=self.quantity)
